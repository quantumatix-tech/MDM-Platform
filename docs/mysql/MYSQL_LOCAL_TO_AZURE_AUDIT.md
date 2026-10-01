# MySQL Local → Azure Cloud Audit

This audit records Local MySQL to Azure Database for MySQL Flexible Server evidence only. It complements the local audit and MySQL reference guides; Azure → Local results are not used as proof here. Report artifacts establish migration outcome and reported object counts. Where noted, separate target inspection established metadata or runtime behavior; report counts alone do not prove runtime behavior.

## 1. Objective

Exercise a MySQL `FULL` migration from a local MySQL Community Server to Azure Database for MySQL Flexible Server. The audit distinguishes successful migration/report evidence from local-only runtime coverage and from Azure behavior blocked by target policy or configuration.

## 2. Environment

### Local MySQL Source

| Item | Value |
|---|---|
| Engine | MySQL Community Server 26.7.0 (as recorded for the audit) |
| Database | `mysql_migration_source` |
| Historical source user | `mysql_test` (as recorded for the audit) |

### Azure MySQL Target

| Item | Value |
|---|---|
| Service | Azure Database for MySQL Flexible Server 8.4.7-azure (as recorded for the audit) |
| Host | `mysql-mdm-migration-test.mysql.database.azure.com` |
| Port | 3306 |
| Database | `mysql_migration_target` |
| Historical target user | `mysql_admin` (as recorded for the audit) |
| Transport | TLS required for the tested target configuration |

The report JSON records MySQL on both ends, run mode, phases, counts, and outcome; it does not record endpoint identity or server version. The host/version/user attribution above comes from the audit's environment notes, not those JSON fields. Secrets are intentionally omitted.

**Configuration direction requires care.** In the current checkout, `config/mysql_local_test.yaml` points from Azure `mysql_migration_source` to local `mysql_migration_target`; it is Azure → Local and is not the reproduction config for this audit. `config/mysql_onpremise_cloud_test.yaml` points from `192.168.1.2` / `retail_customerdb` to the Azure target above. The historical `6b040...` report does not record its config path, so the repository cannot prove that the current config reproduces the historical source endpoint/database/user exactly.

## 3. Test Fixture / Scope

The primary FULL report records 13 tables and 55 source rows, along with columns, primary keys, auto-increment attributes, indexes, unique and check constraints, foreign keys, generated columns, defaults, partitions, comments, views, routines, triggers, and an Event. It reports zero source grants, so security/grant migration was not exercised by this run. This is a description of this fixture, not a claim that every MySQL object variation is Azure-verified.

## 4. Audit Method

1. Review the migration JSON/HTML report and JSONL phase log for run ID, mode, outcome, table rows, object counts, and validation results.
2. Compare reported source and target row counts and inspect the report's partition test results.
3. For selected Azure objects, inspect `INFORMATION_SCHEMA` and `SHOW CREATE` output; treat those checks separately from runtime checks.
4. Record server-policy and connectivity blockers without changing Azure policy to manufacture a pass.

The primary artifact reports FULL table clearing/loading for the configured 13-table set, count validation for every table, and matching counts. It does not assert that every target object outside that managed set is deleted.

## 5. Local → Azure E2E Results

**Primary FULL run:** [`6b0407345782400f861be4927a409fd0`](../../reports/6b0407345782400f861be4927a409fd0.json)

| Measure | Reported result |
|---|---:|
| Mode / status | `full` / `success` |
| Duration | 43.34 seconds |
| Tables | 13 migrated; 0 failed |
| Rows | 55 source rows; 55 migrated; 0 failed |
| Count validation | All 13 table counts matched |
| Views | 2 migrated |
| Functions / procedures | 2 / 2 migrated |
| Triggers / Events | 2 / 1 migrated |
| Indexes / partitions | 8 / 3 migrated |
| Other reported categories | 84 columns, 13 primary keys, 11 auto-increment attributes, 3 unique constraints, 2 foreign keys, 4 check constraints, 2 generated columns, 15 defaults, 8 comments |

The JSON report marks partition testing PASS for `tbl_partition_test`: RANGE method, expression `year(created_at)`, three partitions with matching source/target structure and three rows. The JSONL log independently records successful connection roles for source and target and the phases for table clearing/loading. The report's SUCCESS is evidence for this run and fixture, not universal compatibility or runtime proof for all objects.

## 6. Object / Capability Verification

| Capability | Local implementation / runtime evidence | Azure evidence and boundary |
|---|---|---|
| Tables and data | FULL load and count validation implemented | 13 tables / 55 rows; all table counts match in primary report |
| Partitions | Metadata is read from `INFORMATION_SCHEMA.PARTITIONS`; implementation preserves partition method, expression, names, and bounds | Primary report records 3 partitions and a PASS source/target partition signature check for one table; broader partition runtime coverage is not established |
| Indexes | Index type and definitions are preserved by the MySQL connector | 8 reported migrated in Azure; report counts do not establish index query behavior; runtime coverage is narrower than local |
| Views | Selective source-qualifier rewriting is implemented | 2 reported created; broader Azure view runtime coverage is not established |
| Functions / procedures | SHOW CREATE extraction and definer handling are implemented | 2 of each reported migrated; selected Azure runtime checks were recorded for the tested fixture only |
| Triggers | Trigger definitions are captured and recreated around FULL data loading | 2 reported migrated; selected Azure INSERT/UPDATE behavior was checked; this does not cover every trigger configuration |
| Events | Event metadata snapshot and safe definition recreation are implemented | Primary run reports 1; separate runs below verify selected definition/state/schedule properties. Automatic Azure execution is blocked/unverified |
| Cross-engine type safety | Explicit target mapping is required; unmapped cross-engine types raise `UnmappedTypeError`, with no unsafe source-native fallback | **Implementation: PASS. Azure type-matrix coverage: PARTIAL.** The primary same-engine MySQL run is not broad cross-engine type evidence |
| Error isolation | `migration.stop_on_error` defaults to false; object failures are recorded and eligible independent work can continue with partial success. Setting it true enables fail-fast behavior | **Implementation: PASS. Exhaustive Azure fault-path coverage: PARTIAL.** |
| FULL target handling | Clears/loads the configured migrated table set using the implemented FK-aware process | Primary report lists the 13 cleared tables. FULL does not blindly prune unmanaged target-only objects; Events have no ownership registry and target-only Events are intentionally retained |

### Additional Event-specific Local → Azure Evidence

These are separate FULL runs focused on Event definitions; they are not additional counts for the primary run.

| Run ID | Report result | Separate target evidence recorded |
|---|---|---|
| [`b1c3002060db4e7cae8759aa57ff7b56`](../../reports/b1c3002060db4e7cae8759aa57ff7b56.json) | Success; 2 Events reported migrated; 0 blocked/failed | Recurring Event definition, enabled state, schedule, and rewritten `DEFINER=mysql_admin@%` inspected with target metadata / `SHOW CREATE` |
| [`c36d4e89a58a43acae41e10165649f2a`](../../reports/c36d4e89a58a43acae41e10165649f2a.json) | Success; 3 Events reported migrated; 0 blocked/failed | Future one-time Event's `EXECUTE_AT`, enabled state, and definition inspected on target |
| [`740ba5585c394826acd9257260c3332e`](../../reports/740ba5585c394826acd9257260c3332e.json) | Success; 1 Event reported migrated | A source Event replacement was recorded; older target-only Events remained, as intended without a managed-Event ownership registry |

The reports support the Event counts and successful migration outcome. The definition/state/schedule observations are separate inspection evidence in the audit record, not fields proven by report counts alone. A due one-time Event observed already disabled on the source demonstrates the timing risk; it is not live proof of DMS blocking an enabled due Event.

## 7. MySQL-Specific Azure Findings

### Definer Handling

An Azure Error 1449 exposed a missing source definer account on the target. The implementation now takes the authoritative definition from `SHOW CREATE`, supports optional `routine_definer`, and otherwise uses the cached target `CURRENT_USER`. It narrowly rewrites the `CREATE DEFINER` clause for routines, triggers, and Events; it does not rewrite routine bodies, literals, comments, or unrelated references. For the tested fixture, `mysql_admin@%` and selected target runtime behavior were verified. Definer compatibility is verified for that fixture only, not every security configuration.

### Error 1419 / Binary Logging

When `log_bin=ON` and `log_bin_trust_function_creators=OFF`, MySQL can reject function or trigger creation with Error 1419. An administrator applied `SET PERSIST log_bin_trust_function_creators = ON` and verified persistence for the recorded Azure test. This is an environment/server-policy prerequisite: DMS does not change it, grant `SUPER`, or disable binary logging. The platform reports the blocked condition with remediation guidance.

### Events / Event Scheduler

| Area | Status | Evidence / interpretation |
|---|---|---|
| Event definition and selected state/schedule preservation | **PASS for tested cases** | Primary report and separate Event-specific runs; selected target metadata and `SHOW CREATE` were inspected |
| Azure automatic Event runtime | **NOT VERIFIED / BLOCKED BY TARGET CONFIGURATION** | Azure reported `event_scheduler=OFF`; preserving `STATUS=ENABLED` does not start the server scheduler |
| Scheduler enable attempt | **Blocked** | `SET GLOBAL event_scheduler = ON` returned Error 1227 for the tested account, which lacked the required administrative privilege |
| Target-only Event handling | **Intentional retention** | Without a managed-Event ownership registry, FULL replaces source-snapshot Events and does not prune unknown target-only Events |

Do not grant arbitrary `SUPER` or `SYSTEM_VARIABLES_ADMIN` to make this test pass. If Event runtime must be tested, an Azure/server administrator must first confirm that the service/version exposes a supported configuration path, enable it under environment policy, and verify `SHOW VARIABLES LIKE 'event_scheduler'` returns `ON`. Then verify a controlled scheduled side effect separately from migration metadata.

### TLS / Connectivity

The tested Azure configuration requires TLS on port 3306. Run `4948680233ce49c8bbf696fcc8fae643` failed before migration with MySQL connection timeout 10060 after a laptop restart. Later port-3306 connectivity and a TLS-required MySQL CLI connection succeeded. This is recorded as a network/environment event, not evidence of a migration implementation defect.

## 8. Cloud-Specific Limitations

- Azure runtime evidence is narrower than local runtime coverage for indexes, views, partitions, and recurring Events. Azure migration counts or metadata inspection do not establish query behavior or scheduled execution.
- Event execution could not be verified while the Azure server scheduler was OFF and the test account could not enable it.
- Routine and trigger creation depends on Azure binary-logging policy and compatible definers. Error 1419 and Error 1449 are environment/target compatibility conditions that the platform must report; this audit does not claim they are automatically remediated in every server.
- The primary report has zero source grants. It does not establish Azure user/grant migration behavior.
- The historical primary report does not record its config path or endpoint details, and the current `mysql_local_test.yaml` is reverse-direction. Reproduction must use a verified Local → Azure config and record its actual source/target details with the run.

## 9. Cross-Cutting Issues / Run History

| Finding | Status | Evidence / interpretation |
|---|---|---|
| Primary Local → Azure FULL | **PASS for recorded fixture** | Run `6b040...`: 13 tables, 55 rows, reported object counts, zero failed categories; report status success |
| Event-specific migrations | **PASS for tested definitions** | Runs `b1c300...` and `c36d4...`; definition/state/schedule inspection is separate from count evidence |
| Error 1419 | **Environment/server policy** | Administrator persisted `log_bin_trust_function_creators=ON`; DMS does not change global policy |
| Error 1449 / DEFINER | **Compatibility fix; tested fixture verified** | Narrow `CREATE DEFINER` rewrite to the tested target account, with selected runtime checks |
| Event scheduler OFF / Error 1227 | **Blocked by target configuration** | Event automatic execution not verified; no arbitrary privilege grant recommended |
| Timeout 10060 | **Network/environment event** | Failed connect phase; later port and TLS CLI checks succeeded |
| Type mapping and error isolation | **Implementation PASS; Azure coverage partial** | Implementation behavior is distinct from exhaustive cloud fault/type testing |
| Target-only Events | **Intentionally retained** | No ownership registry exists to distinguish DMS-managed Events from unrelated target Events |

## 10. Final Assessment

The documented Local → Azure FULL path was successfully exercised for the recorded fixture: 13 tables and 55 rows migrated with matching table counts and zero reported failures, alongside reported migration of selected MySQL object categories. The repository report verifies outcome and counts; separate inspection supports selected Azure metadata and runtime claims. Several capabilities have stronger local runtime coverage than Azure runtime evidence, and automatic Event execution remains blocked by target configuration. This audit does not claim universal MySQL object compatibility or production readiness.

## 11. Reproduction

1. Verify the local MySQL source and the Azure MySQL Flexible Server target, including database names and account privileges.
2. Select a configuration whose `source` is Local MySQL and `target` is Azure MySQL. In the current checkout, `config/mysql_onpremise_cloud_test.yaml` has that direction, but its source values are `192.168.1.2` / `retail_customerdb` / `eds_remote`; adjust only in your local working configuration if your intended fixture differs. Do not use `config/mysql_local_test.yaml` for Local → Azure; it is Azure → Local.
3. Supply configured secrets through the environment; do not put secret values in this document or commit them.
4. Confirm network access to Azure port 3306 and TLS. Check target privileges and the Error 1419 prerequisite if routines/triggers are in scope.
5. Run the selected verified Local → Azure config, for example:

   ```powershell
   python -m migration_platform `
     --config config\mysql_onpremise_cloud_test.yaml `
     --mode full
   ```

6. Record the generated run ID; inspect its JSON, HTML, and JSONL artifacts. Compare source/target row counts and object counts, and use `SHOW CREATE` / `INFORMATION_SCHEMA` for selected objects.
7. Where applicable, test functions, procedures, and triggers with controlled calls/DML. Check Event definition/state and scheduler state separately; do not treat `STATUS=ENABLED` as proof that the scheduler executes Events.
8. Do not change Azure server policy solely to manufacture a passing result. If an administrator-supported setting is changed under environment policy, record it as a prerequisite and separately verify the resulting behavior.

## 12. References

- [Local MySQL audit](MYSQL_LOCAL_AUDIT.md)
- [MySQL limitations](MYSQL_LIMITATIONS.md)
- [MySQL object support matrix](MYSQL_OBJECT_SUPPORT_MATRIX.md)
- [MySQL test guide](MYSQL_TEST_GUIDE.md)
- [MySQL E2E runbook](MYSQL_E2E_RUNBOOK.md)
- [MySQL migration flow](MYSQL_MIGRATION_FLOW.md)
- Current Local → Azure config: `config/mysql_onpremise_cloud_test.yaml`
- Current reverse-direction config: `config/mysql_local_test.yaml`
