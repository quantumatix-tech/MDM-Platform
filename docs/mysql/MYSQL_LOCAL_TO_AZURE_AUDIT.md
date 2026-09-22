# MySQL Local → Azure Cloud Audit

This dedicated Local → Azure audit complements [MYSQL_LOCAL_AUDIT.md](MYSQL_LOCAL_AUDIT.md) and [MYSQL_LIMITATIONS.md](MYSQL_LIMITATIONS.md). It does not replace either. Some recorded work predates the current Codex session and was completed through earlier Copilot/manual work. Only results explicitly labelled Local → Azure are Azure evidence.

## Objective and environment

| Item | Value |
|---|---|
| Project / branch | Unified DMS / MDM-Platform / `feature/unified-dms-platform` |
| Source | Local MySQL Community Server 26.7.0; `mysql_migration_source`; `mysql_test` |
| Target | Azure MySQL Flexible Server 8.4.7-azure; `mysql-mdm-migration-test.mysql.database.azure.com`; `mysql_migration_target`; `mysql_admin` |
| Config / command | `config/mysql_local_test.yaml`; `python -m migration_platform --config config/mysql_local_test.yaml --mode full` |

Secrets are omitted. The same MySQL code path is used for Local/Azure; Azure uses TLS-required connection behavior. Evidence used reports, source/target metadata, SHOW CREATE, target definers, and runtime where permitted. Metadata alone is not runtime proof.

## Tasks 1–3 — Core safety, FULL behavior, lifecycle

| Task | Problem / root cause | Fix / implementation | Azure status |
|---|---|---|---|
| 1 | Cross-engine MySQL/MSSQL could fall back from `col.target_type` to source-native `col.source_type`. | `UnmappedTypeError` requires explicit mapping; same-engine MySQL stays native; CLI passes source-engine metadata. Generic Phase-0 error isolation, `stop_on_error`, and PostgreSQL WAL restart safety are not Azure MySQL features. | **PASS implementation; PARTIAL Azure type matrix** |
| 2 | One object failure could stop unrelated work. | `stop_on_error` defaults false; per-object failures yield `partial_success`; true stops first failure. FULL uses DELETE and connection-scoped FK checks for known tables, then parent-before-child loading. | **PASS implementation; PARTIAL exhaustive Azure fault paths** |
| 3 | Read transactions/unclosed connectors caused metadata locks/sleeping sessions; trigger DDL lacked reliable rollback. | Source autocommit; FULL/CDC/assessment rollback-and-close cleanup; failed trigger DDL rollback; no SUPER/PROCESS/manual termination. | **PASS; platform defect fixed, not Azure limitation** |

## Tasks 4–6 — Tables, indexes, views

| Task | Fix / implementation | Evidence / status |
|---|---|---|
| 4 | `INFORMATION_SCHEMA.PARTITIONS` preserves RANGE/RANGE COLUMNS/LIST/LIST COLUMNS/HASH/KEY, expressions, ordered names/bounds, MAXVALUE in CREATE TABLE. Opt-in `reconcile_target_schema` stages/verifies/swaps source-derived tables, retains `__dms_backup_*`, and blocks unmanaged inbound FKs. | Local `4c78ab8ea2724257a64fc2209dcfdb66`: 10 tables, 43/43 rows, three verified partitions. Azure FULL reports 3 partitions but no separate Azure partition runtime. **PASS implementation; PARTIAL Azure runtime**. |
| 5 | `INDEX_TYPE` is retained in model/DDL/equivalence so FULLTEXT/SPATIAL work correctly. | Local metadata/runtime covered normal, unique, composite, FULLTEXT/SPATIAL, MATCH, POINT, ST_Within. Azure FULL reports 8 indexes; no Azure index runtime evidence. **PASS implementation/Local runtime; PARTIAL Azure runtime**. |
| 6 | Rewrite only source-qualified references to migrated objects; never blindly rewrite literals/comments/external references. | Local target-local views/runtime verified. Azure `6b0407345782400f861be4927a409fd0` reports 2 views; no separate Azure view inspection/runtime. **PASS implementation/Local runtime; PARTIAL Azure runtime**. |

## Tasks 7–8 — Routines, definers, triggers

| Task | Problem / root cause | Fix / implementation | Azure status |
|---|---|---|---|
| 7 | Wrong SHOW CREATE field; missing `mysql_test@%` on Azure caused actual ERROR 1449. | Authoritative SHOW CREATE extraction; optional `routine_definer`, otherwise cached CURRENT_USER; narrow rewrite only of CREATE DEFINER for routines/triggers/Events, not body/literals/comments. | `6b0407345782400f861be4927a409fd0`: 2 functions/2 procedures, runtime and `mysql_admin@%` definers verified. **PASS** |
| 8 | Lifecycle issue plus ERROR 1419 when binary logging ON and trust setting OFF. | DMS reports BLOCKED and never changes global setting/logging or grants SUPER. Administrator applied `SET PERSIST log_bin_trust_function_creators = ON;`; persistence verified. | Azure trigger metadata, SHOW CREATE, `mysql_admin@%`, INSERT/UPDATE runtime verified in `6b0407345782400f861be4927a409fd0`. **PASS after server-policy prerequisite** |

## Task 9 — Events

**Problem/root cause:** Late discovery captured only name plus SHOW CREATE; an enabled preserved one-time Event could fire and become DISABLED before capture.

**Fix:** Immediately after connection, DMS snapshots DDL, EVENT_TYPE, STATUS, EXECUTE_AT, interval, STARTS, ENDS, ON_COMPLETION, TIME_ZONE, definer, and timestamp. Later creation reuses the snapshot. Default `migration.one_time_event_safety_lead_seconds=300`. Enabled one-time Events due/near-due at snapshot or target creation are `EVENT: BLOCKED` before DROP EVENT; same-name targets are untouched. Recurring state and safe target-definer rewriting remain preserved.

| Evidence | Result |
|---|---|
| `b1c3002060db4e7cae8759aa57ff7b56` | 2 Events migrated, 0 blocked/failed/errors. `evt_task9_recurring_enabled` source/target: RECURRING, ENABLED, EVERY 1 MINUTE, STARTS `2026-09-21 14:42:11`, no end, PRESERVE. Target SHOW CREATE retained action/schedule and `DEFINER=mysql_admin@%`. |
| `c36d4e89a58a43acae41e10165649f2a` | 3 Events migrated, 0 blocked/failed/errors. `evt_task9_one_time_future` source/target: ONE TIME, ENABLED, EXECUTE_AT `2026-09-21 14:55:55`, PRESERVE. Target SHOW CREATE retained exact AT schedule/action/ENABLE/definer. |
| Due/past observation | `evt_task9_one_time_due` was already DISABLED through MySQL before DMS observed it. This proves timing risk, not live proof of enabled-due DMS blocking; that path is unit tested. |
| Scheduler | Azure `event_scheduler=OFF`: metadata/state is PASS; automatic target runtime is **BLOCKED / NOT EXECUTED**. STATUS=ENABLED is not scheduler ON. |
| `740ba5585c394826acd9257260c3332e` | After source retained only `evt_migration_e2e_final`, Events migrated=1, blocked=0, failed=0. Target-only old Events remained intentionally: no managed-Event ownership registry exists, so FULL creates/replaces source-snapshot Events only and must not prune by name/prefix. |

**Task 9 status:** **PASS** metadata/state/schedule; **BLOCKED / NOT EXECUTED** Azure runtime; **PASS** intentional target-only retention.

### Azure Event Scheduler Runtime Limitation

Event definition migration is **PASS**. Tested recurring Events retained their
enabled state and schedules; future one-time Events retained `ENABLED` and their
future `EXECUTE_AT`; and target-definer rewriting was verified. Azure automatic
Event execution is **NOT VERIFIED / BLOCKED BY TARGET CONFIGURATION**.

The Azure target reported `event_scheduler=OFF` through:

```sql
SHOW VARIABLES LIKE 'event_scheduler';
```

An attempted `SET GLOBAL event_scheduler = ON;` returned `ERROR 1227 (42000)`:
the current `mysql_admin` account lacks `SUPER` or `SYSTEM_VARIABLES_ADMIN`.
This is a managed Azure server/configuration limitation, not a DMS Event
migration defect. DMS must not silently enable `event_scheduler`; it is a
server-level operational setting under Azure/server administrator control.

`EVENT.STATUS=ENABLED` means the Event definition is enabled.
`event_scheduler=ON` means the server scheduler is active. Both are required
for automatic execution. Preserving Event `ENABLED` does not prove runtime.

#### Future test / environment prerequisite

Before runtime testing, verify whether this Azure MySQL Flexible Server/version
exposes `event_scheduler` as a supported configurable server parameter in the
actual Azure administration interface. Do not assume a Portal path or that every
server/version exposes it. If it is supported and requires an Azure
administrator, have that administrator enable it through Azure/server
configuration; do not grant arbitrary `SUPER` or `SYSTEM_VARIABLES_ADMIN` to
`mysql_admin` merely for this test. Reconnect and verify:

```sql
SHOW VARIABLES LIKE 'event_scheduler';
```

Expected result: `ON`. Then run a controlled future-scheduled Event and verify
its target-database side effect, recording the result/date in this audit.

| Condition | Meaning | Action |
|---|---|---|
| `event_scheduler=OFF` | Server scheduler inactive | Azure administrator must enable supported server configuration before runtime test. |
| `SET GLOBAL ... = ON` returns ERROR 1227 | Current account lacks required privilege | Do not grant arbitrary SUPER; use Azure/server administration. |
| Event `STATUS=ENABLED` + scheduler OFF | Definition enabled, scheduler inactive | Migration is not necessarily faulty; runtime cannot execute/verify. |
| Event `STATUS=ENABLED` + scheduler ON | Runtime can be tested | Run controlled scheduled-Event test. |

#### Future verification checklist

- [ ] Confirm Azure supports/configures `event_scheduler` for this server/version.
- [ ] Enable it through Azure/server administration.
- [ ] Reconnect to MySQL.
- [ ] Verify `SHOW VARIABLES LIKE 'event_scheduler'` returns `ON`.
- [ ] Verify a recurring migrated Event is `STATUS=ENABLED`.
- [ ] Verify its scheduled side effect occurs.
- [ ] Record runtime evidence.
- [ ] Restore/disable scheduler only if test-environment policy requires it.

## Cross-cutting Cloud issues and run history

| Item | Classification / outcome |
|---|---|
| `4948680233ce49c8bbf696fcc8fae643` | Environment failure: Azure timeout 10060 after laptop restart. Later port-3306 test and TLS-required MySQL CLI succeeded. |
| `6b0407345782400f861be4927a409fd0` | FULL PASS: 13 tables, 55 rows, 0 failed/errors, 2 functions, 2 procedures, 2 triggers, 1 Event, 8 indexes, 3 partitions, 2 views. |
| TLS / target privilege | Environment/configuration: TLS required; required object privileges only, no broad administration claim. |
| Definer difference | Migration defect fixed: Error 1449 resolved by narrow rewrite to `mysql_admin@%`. |
| Error 1419 | Server policy: administrator SET PERSIST; DMS does not auto-remediate global policy or use SUPER. |
| Jenkins port 8080 | Local tooling issue: Jenkins interfered with UI/report server; strict port-8080 behavior replaced random fallback. |

## Evidence, limits, and reproduction

## Task 13 — allowlisted users, roles, and grants

Task 13 uses an explicit allowlist: `security_test_user@%`,
`reporting_role@%`, and `read_role@%`. Passwords, hashes, and authentication
secrets are neither queried nor migrated. MySQL 26.7 has no
`mysql.user.is_role`; the connector uses the allowlist to classify scoped
principals and never scans every server account.

| Evidence | Observed result |
|---|---|
| Local MySQL 26.7 → Azure `39c9933250be4f2daadacaf44ccdc45f` | **SUCCESS**; 28.2 seconds; 23 tables, 115 rows, 0 failed, 0 errors. |
| Target principals | `read_role@%`, `reporting_role@%`, `security_test_user@%` exist. |
| Target edges | `read_role@% → reporting_role@%`; `reporting_role@% → security_test_user@%`; both `WITH_ADMIN_OPTION=N`. |
| Target grants | `read_role@%`: SELECT on target database; `reporting_role@%`: read role; user: direct SELECT plus reporting role. Each showed USAGE. |
| Behavior as the user | SELECT succeeded (16); CREATE, INSERT, UPDATE returned ERROR 1142 (denied). |

DELETE, ALTER, DROP, and GRANT behavior is **NOT YET EXECUTED**. The direct
user SELECT grant means SELECT does not independently prove inheritance-only
access. An earlier temporary INSERT grant was revoked and is not final
evidence. Local → Local Task 13 security-principal end-to-end verification is
**NOT YET EXECUTED**.

```sql
SELECT EVENT_NAME, EVENT_TYPE, STATUS, EXECUTE_AT, INTERVAL_VALUE,
       INTERVAL_FIELD, STARTS, ENDS, ON_COMPLETION
FROM INFORMATION_SCHEMA.EVENTS WHERE EVENT_SCHEMA = '<database>';
SHOW CREATE EVENT <event_name>;
SHOW CREATE TRIGGER <trigger_name>;
SHOW CREATE FUNCTION <function_name>;
SHOW CREATE PROCEDURE <procedure_name>;
SHOW VARIABLES LIKE 'event_scheduler';
```

No secrets are documented and no Azure target object was manually created, altered, or deleted to make a result pass. Azure Event automatic runtime remains **NOT VERIFIED / BLOCKED BY TARGET CONFIGURATION** while `event_scheduler=OFF`; the current `mysql_admin` account received ERROR 1227 when attempting `SET GLOBAL event_scheduler = ON`. Azure runtime evidence is narrower than Local→Local for indexes/views/partitions/recurring Events. Function/trigger creation depends on server policy. FULL does not delete unknown target-only Events.

To reproduce: provide configured secrets without documenting them, confirm Azure TLS/network access to port 3306, run the command above, inspect reports plus source/target metadata and SHOW CREATE, and do not alter scheduler/global settings or prune target-only Events.
