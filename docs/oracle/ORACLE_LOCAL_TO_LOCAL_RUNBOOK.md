# Oracle Local-to-Local Migration Runbook

This is the operational sequence for migrating an existing, manually populated
Oracle SOURCE schema to an approved TARGET. It is separate from disposable
fixture E2E onboarding. Examples use the current canonical profile; different
environments require their reviewed config values.

## 1. Workflow and current profile

1. A DBA prepares the SOURCE/TARGET PDBs, services, approved permanent storage,
   and owner accounts.
2. SOURCE contains the data and supported objects. TARGET is the approved
   migration owner; its current state is inspected, not assumed empty.
3. Open a repository terminal, activate Python, and restore this process's
   configured secrets.
4. Run owner authentication, read-only metadata compatibility, and the normal
   Oracle overwrite planner.
5. Review all blockers, target inventory, dependencies, and grants.
6. After explicit approval, run one FULL migration; inspect its run report and
   perform independent validation.

Current config endpoints are `ORACLE_SOURCE_DB` / `MIGE2E_SIMPLE_SRC` and
`ORACLE_TARGET_DB` / `MIGE2E_SIMPLE_TGT`; configured CDB unique name is `FREE`.
The current canonical config enables permission migration, so the dedicated
TARGET-local executor is also a FULL prerequisite. These are config values,
not framework constants.

## 2. One-time database preparation and minimum authority

The platform migrates schemas; it does not create/drop Oracle CDBs or PDBs.
PDB creation, service registration, permanent storage placement, and initial
DBA authorization are infrastructure work. Do not use SYSTEM/SYSAUX for
application storage or invent paths, sizes, quotas, identities, or passwords.

SOURCE owner: must authenticate to SOURCE, own/read its objects, and have
privileges required for its own DDL and supported catalog discovery. If
permission migration is enabled, source grant discovery requires the approved
read-only access to `SYS.DBA_USERS`, `SYS.DBA_ROLES`, `SYS.DBA_ROLE_PRIVS`, and
`SYS.DBA_SYS_PRIVS`. Normal FULL does not create/reset this owner.

TARGET owner: must authenticate to TARGET and have appropriate tablespace
quota. Existing target-only objects are preserved unless they conflict with
source-managed changes or dependencies. Normal FULL does not create/reset this
owner. The current config authorizes the existing bootstrap to grant only
missing `CREATE VIEW`, `CREATE SEQUENCE`, or `CREATE TRIGGER` privileges to the
TARGET owner when source discovery requires them. It verifies target PDB
identity and requires the configured admin to hold direct ADMIN OPTION on
each exact grant; otherwise it fails closed. It does not grant these to SOURCE.

Permission executor: the current profile references the TARGET-local
`ORACLE_PERMISSION_EXECUTOR` and `oracle_permission_executor_password`.
Existing accounts are reused. Normal FULL does not create a missing executor
or reset its password. When permission migration is enabled, its catalog
visibility is probed for `DBA_USERS`, `DBA_ROLES`, `DBA_ROLE_PRIVS`,
`DBA_SYS_PRIVS`, `DBA_TAB_PRIVS`, and `DBA_COL_PRIVS`. Missing/locked/wrong-PDB
executor state must be resolved through separately approved onboarding.

TARGET replacement inspection may separately need `DBA_DEPENDENCIES` and
`DBA_CONSTRAINTS`; act only on exact planner/preflight findings. The E2E DBA
bootstrap is explicit and approval-gated; ordinary FULL does not silently
provision accounts or catalog grants. Never grant `DBA`, `SELECT ANY
DICTIONARY`, `GRANT ANY PRIVILEGE`, or broad role authority as a workaround.

## 3. Prepare SOURCE; understand discovery scope

Create/import real application tables, data, keys, indexes, views, sequences,
routines, triggers, synonyms, and grants through the organization's approved
process. Oracle source discovery is owner-scoped and does not require a YAML
entry for every production table. An explicit `source.connection.include_tables`
can narrow table scope; it is absent from the current acceptance YAML.

`e2e.fixtures`, `e2e.expected.objects`, and `e2e.expected.row_counts` are for
fixture loading and acceptance, not production table selection. Do not load
fixtures into a populated SOURCE owner. The supported migration path includes
tables/PK/FK, supported B-tree indexes and scalar/LOB values, ordinary views,
partial standalone sequence state, supported table DML triggers,
supported functions/procedures/packages, restricted private owner synonyms,
and the explicitly supported permission subset after its preflight succeeds.

Consult `ORACLE_OBJECT_SUPPORT_MATRIX.md` for limits. Examples of unsupported
or partial cases include nested TSTZ in OBJECT/VARRAY, database event and
INSTEAD OF triggers, public/database-link synonyms, unsupported cross-owner
dependencies, and some materialized views. FULL must fail closed rather than
silently skip unsupported source objects. Target sequence state is monotonic;
exact CURRVAL equality is not promised and sequences are not rewound.

## 4. Reopen terminal and restore secrets

Follow [ORACLE_TERMINAL_REOPEN_RUNBOOK.md](ORACLE_TERMINAL_REOPEN_RUNBOOK.md)
for PowerShell setup and secure prompts. The current shared provider is `env`,
which resolves `SECRET_<password_secret>` from the current process. Restore
SOURCE owner, TARGET owner, and executor credentials each new terminal (unless
the executor resolves from its encrypted Local Store). TARGET admin secret is
needed only if a configured target privilege bootstrap actually has a missing
grant to apply. SOURCE/TARGET onboarding-admin secrets and SQL*Plus/SYSDBA are
not routine FULL requirements.

## 5. Read-only checks for populated schemas

The following commands inspect existing schemas without an empty-owner gate.
Run them from the repo root after restoring credentials.

**READ-ONLY — owner login and PDB identity.**

```powershell
python -m tests.e2e.oracle.check_metadata_compatibility --config config/oracle_e2e_acceptance.yaml --auth-only
```

**READ-ONLY database inspection; writes a sanitized details JSON file.**

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
python -m tests.e2e.oracle.check_metadata_compatibility --config config/oracle_e2e_acceptance.yaml --details-file "reports/oracle_metadata_$stamp.json"
```

**READ-ONLY — normal Oracle FULL replacement plan.**

```powershell
python -m migration_platform --config config/oracle_e2e_acceptance.yaml --mode full --oracle-overwrite-plan
```

Review object scope, blockers, `inspection_errors`, target-only preservation,
dependencies, and proposed destructive DDL. Do not proceed on an identity
mismatch, inaccessible catalog, unsupported object, unverified target
inventory, or unresolved dependency.

Do not use `python -m tests.e2e.oracle.run_preflight` or
`scripts/oracle_live_e2e.ps1 -PreflightOnly` for a populated source. The current
E2E source mode is `fixtures`: setup requires an empty SOURCE and the fixture
preflight requires an empty TARGET. `Existing SOURCE owner is not empty` is
expected in that fresh-fixture path. Do not clean the owner to bypass it.

## 6. Run normal FULL once

FULL replaces rows only in source-managed TARGET tables and reconciles
supported source-managed objects under the safety planner. Target-only objects
outside the managed scope remain preserved. `cleanup.mode: never` does not
authorize broad drops. Routine managed-row replacement needs no per-run
`approval_id` when inspection succeeds and no separately protected schema
cleanup is proposed. A changed plan must be reviewed again.

**DATABASE MUTATION / REQUIRES EXPLICIT APPROVAL — performs FULL migration.**

```powershell
python -m migration_platform --config config/oracle_e2e_acceptance.yaml --mode full --no-live-ui --exit-after-run
```

Oracle DDL commits implicitly and data is committed in steps; a later error
can leave partial target changes. A failure is not permission to retry blindly.
The default CLI starts a status server bound to `0.0.0.0`; the flags above
disable Rich UI and exit after the report is written.

## 7. Independent validation

Run the Oracle E2E validator only when the config's expected table/row scope
matches these schemas. It performs read-only comparisons and writes a report;
it does not reload data.

**READ-ONLY database validation; report-file write only.**

```powershell
python -m tests.e2e.oracle.run_validation --config config/oracle_e2e_acceptance.yaml
```

For non-acceptance production schemas, the current simple fixture expectations
may be wrong. Use a matching validation config or metadata checks; never edit
fixture expected counts to manufacture PASS. Inspect the report result and
every failed check. For the canonical simple acceptance, the expected tables
are `SIMPLE_CUSTOMER` (2 rows) and `SIMPLE_ORDER` (3 rows); extra supported
non-table objects do not expand that table list.

## 8. Repeat FULL safely

Before each repeat, restore session secrets, rerun `--auth-only`, the full
metadata compatibility check, and `--oracle-overwrite-plan`. Confirm the plan
still scopes only the approved source-managed objects, with no new blockers or
inspection errors. Run FULL once and inspect its report. Never reuse an old
plan against a changed live state.

## 9. SQL*Plus owner checks

Use interactive password prompting; do not place passwords in commands. SQL
statements below are one line each.

**READ-ONLY connection and inventory.**

```powershell
sqlplus MIGE2E_SIMPLE_SRC@//127.0.0.1:1521/ORACLE_SOURCE_DB
```

```sql
SELECT USER, SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME'), SYS_CONTEXT('USERENV','DB_UNIQUE_NAME') FROM dual;
SELECT object_type, object_name, status FROM user_objects ORDER BY object_type, object_name;
SELECT table_name, num_rows FROM user_tables ORDER BY table_name;
EXIT
```

Repeat using the configured TARGET owner/service. Local CDB switching is only
for an authorized DBA; use the SID from the onboarding config and verify CDB
identity before switching to the approved PDBs. `ALTER SESSION SET CONTAINER`
changes session context only. Any future SQL*Plus PL/SQL block needs `/` on a
separate line as a SQL*Plus terminator.

## 10. Errors and recovery

| Condition | Correct action |
|---|---|
| Missing secret | Compare the YAML reference with the name-only env check; restore it in this process or verify the executor store. Never print values. |
| ORA-01017 | Verify username, corresponding secret, and service separately. Owner/admin/executor passwords are not interchangeable. Do not reset accounts without approval. |
| ORA-00942 | Inspect exact failed stage and catalog view. Missing visibility is not an empty result. Request only exact approved access. |
| Wrong PDB/service/CDB | Stop; verify `CON_NAME`, `SERVICE_NAME`, and `DB_UNIQUE_NAME`. Never fall back to another PDB. |
| Nonempty SOURCE during E2E preflight | Use populated-schema checks above, not fixture setup. |
| FULL failed or PARTIAL_SUCCESS | Preserve state. Inspect `reports/<run_id>.json`, `logs/<run_id>.jsonl`, and read-only TARGET inventory before any retry. Oracle DDL is not rolled back. |
| Permission migration failure | Keep the approved feature enabled. Resolve exact source catalog, executor, or target authority gaps; do not silently skip permissions. |

## 11. Fresh fixture E2E is separate

Fresh E2E uses disposable owners, checks emptiness, loads canonical fixtures,
runs the shared FULL CLI, and validates. It is not the resume path for
populated manual schemas. `-PreflightOnly` is read-only but rejects non-empty
fixture owners by design. The full launcher may execute owner/storage/catalog
setup only under its explicit approval plan; never point it at protected
application schemas.

## 12. Peer-engine workflow comparison

MySQL, PostgreSQL, MSSQL, and Oracle all use YAML `password_secret`, the shared
provider factory, and `SECRET_<reference>` when configured with the env
provider. The peer docs use `python -m migration_platform --config ... --mode
full`; their existing-target handling is engine-specific. Oracle adds PDB
identity checks, potentially separate PDB-local owners/executor, and explicit
catalog visibility/privilege preflight. SQL*Plus/SYSDBA belongs only to
optional local DBA onboarding. Ordinary Oracle FULL still uses the shared CLI
and does not require fixtures or PDB provisioning.

The current MySQL documentation has no separate terminal or local-to-local
runbook under `docs/mysql`; its relevant references are
`docs/mysql/MYSQL_OBJECT_SUPPORT_MATRIX.md` and
`docs/mysql/MYSQL_EXISTING_TARGET_HANDLING.md`. PostgreSQL's operational
references are `docs/postgresql/POSTGRESQL_E2E_RUNBOOK.md`,
`docs/postgresql/POSTGRESQL_TEST_GUIDE.md`, and
`docs/postgresql/POSTGRESQL_EXISTING_TARGET_HANDLING.md`. MSSQL's are
`docs/mssql/MSSQL_E2E_RUNBOOK.md`, `docs/mssql/MSSQL_TEST_GUIDE.md`, and
`docs/mssql/MSSQL_EXISTING_TARGET_HANDLING.md`. Those peer runbooks use the
same environment-backed secret naming pattern and shared FULL CLI; Oracle's
extra steps are PDB identity, executor/catalog readiness, and its optional
PDB-local onboarding path.

See [ORACLE_TERMINAL_REOPEN_RUNBOOK.md](ORACLE_TERMINAL_REOPEN_RUNBOOK.md) for
terminal startup, secrets, reports, and reconnect details.
