# Oracle Setup and Migration Guide

**Local-to-local FULL migration | Windows | Developer runbook**

Verified local profile: `config/oracle_local_to_local.yaml`

Use this guide to prepare Oracle endpoints, provision migration accounts once, and run the normal FULL migration against an existing or newly populated SOURCE schema. No fixture loader, separate setup YAML or DevOps runner is required.

## 1. Quick start

**Already set up?** Open PowerShell in the project directory, activate your existing Python environment if needed, and run these four commands. Enter the existing owner/executor passwords, not the PDB administrator passwords.

```powershell
$env:SECRET_oracle_local_source_owner_pass = Read-Host "SOURCE password"
$env:SECRET_oracle_local_target_owner_pass = Read-Host "TARGET password"
$env:SECRET_oracle_local_permission_executor_pass = Read-Host "Executor password"
python -m migration_platform --config config/oracle_local_to_local.yaml --mode full
```

If this is your first setup, complete the prerequisites below, then follow Sections 2–7 in order. Do not run APPLY before every migration.

| Action | Frequency | Owner |
| --- | --- | --- |
| Prepare SOURCE and TARGET PDBs | Once, if absent | DBA |
| Review YAML and run PLAN / APPLY | Once; repeat only for new authority or recovery | Developer + authorized DBA |
| Create/import application data and objects | As needed | SOURCE owner |
| Set three runtime secrets | Each new PowerShell session | Developer |
| Run FULL; review and verify | Each migration | Developer |

### Prerequisites and terminal setup

Oracle Database installed and running (verified: Oracle AI Database 26ai Free, 23.26.3.0.0); Python 3.11+; repository dependencies; authorized PDB administrators and adequate permanent storage. SQL*Plus is used for local DBA steps, not routine Python Thin-mode FULL.

```powershell
Set-Location "D:\dmt_migration\MDM-Platform"
python --version
python -c "import oracledb, yaml; print(oracledb.is_thin_mode())"
```

If the Python environment is missing, create it once, then activate it on each new terminal. Use the repository-managed environment if one already exists.

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m migration_platform --version
```

For an existing `.venv`, only activate it in a new terminal: `. .\.venv\Scripts\Activate.ps1`. Skip installation if your project environment is already ready.

### Account responsibilities

| Account | Purpose |
| --- | --- |
| PDBADMIN / authorized SYSDBA | Setup only; SYSDBA only for required approved catalog grants. |
| SOURCE_OWNER | Reads application data, metadata and supported grants. |
| TARGET_OWNER | Creates/replaces managed objects and loads data; grants on its own objects. |
| TARGET_EXECUTOR | Creates approved local users/roles and applies approved memberships/system grants. |

## 2. Prepare the PDBs (one time)

Skip creation if approved PDBs already exist. Example values below are from the verified Windows environment: FREE CDB, 127.0.0.1:1521, SOURCE_DB_NEW and TARGET_DB_NEW. Adapt paths and service names to your installation; never reuse another PDB’s files.

### Connect to the root container

```powershell
Get-Service *Oracle*
& "C:\app\admin\product\26ai\dbhomeFree\bin\sqlplus.exe" /nolog
```

```sql
CONNECT / AS SYSDBA
SHOW CON_NAME
SHOW PDBS
SHOW PARAMETER db_create_file_dest
SHOW PARAMETER pdb_file_name_convert
SHOW PARAMETER max_pdbs
SELECT con_id, name FROM v$datafile ORDER BY con_id, name;
HOST DIR C:\APP\ADMIN\PRODUCT\26AI\ORADATA\FREE
```

Confirm CDB$ROOT, inspect existing PDBs, storage and edition limits. MAX_PDBS is not a license limit. Preserve unrelated PDBs. In SQL*Plus run each statement separately.

### Create and open both PDBs

```sql
HOST mkdir C:\APP\ADMIN\PRODUCT\26AI\ORADATA\FREE\SOURCE_DB_NEW
HOST mkdir C:\APP\ADMIN\PRODUCT\26AI\ORADATA\FREE\TARGET_DB_NEW
```

```sql
CREATE PLUGGABLE DATABASE SOURCE_DB_NEW ADMIN USER PDBADMIN IDENTIFIED BY "<SOURCE_PDBADMIN_PASSWORD>" ROLES = (DBA) DEFAULT TABLESPACE USERS CREATE_FILE_DEST = 'C:\APP\ADMIN\PRODUCT\26AI\ORADATA\FREE\SOURCE_DB_NEW';
```

```sql
CREATE PLUGGABLE DATABASE TARGET_DB_NEW ADMIN USER PDBADMIN IDENTIFIED BY "<TARGET_PDBADMIN_PASSWORD>" ROLES = (DBA) DEFAULT TABLESPACE USERS CREATE_FILE_DEST = 'C:\APP\ADMIN\PRODUCT\26AI\ORADATA\FREE\TARGET_DB_NEW';
```

Replace password placeholders privately. Each CREATE statement is a single SQL line (visual wrapping only).

```sql
ALTER PLUGGABLE DATABASE SOURCE_DB_NEW, TARGET_DB_NEW OPEN READ WRITE;
SHOW PDBS
ALTER PLUGGABLE DATABASE SOURCE_DB_NEW, TARGET_DB_NEW SAVE STATE;
SELECT name, network_name, pdb FROM v$services WHERE UPPER(pdb) IN ('SOURCE_DB_NEW', 'TARGET_DB_NEW');
EXIT
```

### Verify listener and service access

```powershell
& "C:\app\admin\product\26ai\dbhomeFree\bin\lsnrctl.exe" status
Test-NetConnection -ComputerName 127.0.0.1 -Port 1521
```

Expected: both PDBs READ WRITE; services `source_db_new` and `target_db_new` READY; TCP test True. Do not create runtime owners or executor manually.

If you need to confirm the two admin passwords before APPLY, run these read-only login checks in the project Python environment (each asks for the relevant PDBADMIN password):

```powershell
python -c "import oracledb; from getpass import getpass; c=oracledb.connect(user='PDBADMIN', password=getpass('SOURCE PDBADMIN password: '), dsn='127.0.0.1:1521/source_db_new'); print('Connection successful'); c.close()"
python -c "import oracledb; from getpass import getpass; c=oracledb.connect(user='PDBADMIN', password=getpass('TARGET PDBADMIN password: '), dsn='127.0.0.1:1521/target_db_new'); print('Connection successful'); c.close()"
```

Both connections were verified in the recorded local setup. PLAN also verifies authenticated endpoint identities.

## 3. Configure the migration

Use config/oracle_local_to_local.yaml for both setup and FULL. This is the tested profile; replace connection/account values for a different approved environment. Use spaces, not tabs, and never store actual passwords in YAML.

```yaml
source:
  engine: oracle
  connection:
    host: 127.0.0.1
    port: 1521
    service_name: source_db_new
    username: SOURCE_OWNER
    password_secret: oracle_local_source_owner_pass
    connection_timeout: 5
    connection_max_retries: 2
    connection_retry_delay: 1

target:
  engine: oracle
  connection:
    host: 127.0.0.1
    port: 1521
    service_name: target_db_new
    username: TARGET_OWNER
    password_secret: oracle_local_target_owner_pass
    connection_timeout: 5
    connection_max_retries: 2
    connection_retry_delay: 1

secrets:
  provider: env

oracle_permission_migration:
  enabled: true
  executor:
    username: TARGET_EXECUTOR
    password_secret: oracle_local_permission_executor_pass
  new_user_policy: LOCKED
  approve_role_creation_admin_option: true
  approve_create_session: true

migration:
  mode: full
  stop_on_error: true
  batch_size: 1000
  reconcile_target_schema: true
  oracle_schema_replacement:
    enabled: true

alerting:
  notifier: none

validation:
  mode: count
  sample_size: 1000

logging:
  level: INFO
```

### What to fill in the YAML

| Setting | What to enter / keep |
| --- | --- |
| `source.engine`, `target.engine` | `oracle` |
| `host`, `port` | Reachable listener address and port (local example: `127.0.0.1`, `1521`). |
| `service_name` | The correct PDB service on each side, not the CDB SID. |
| `username` | Intended SOURCE/TARGET schema owner. APPLY provisions missing accounts. |
| `password_secret` | Secret reference only. With `env`, use `SECRET_` + this value in PowerShell. |
| Connection timeout / retries / delay | Keep `5` / `2` / `1` for the tested local profile; review for other environments. |
| `secrets.provider` | `env` for this guide. |
| `oracle_permission_migration.enabled` | `true` to migrate supported application permissions. |
| `executor.username`, `executor.password_secret` | TARGET-side security executor name and secret reference; uses the TARGET host/service. |
| `new_user_policy` | `LOCKED`: newly migrated application users remain locked. |
| `approve_role_creation_admin_option`, `approve_create_session` | `true` in the tested, approved security policy; authorize only when appropriate. |
| `mode`, `stop_on_error`, `batch_size` | `full`, `true`, `1000` for the tested profile. |
| `reconcile_target_schema`, `oracle_schema_replacement.enabled` | Both `true` for the tested FULL replacement workflow; review target scope before execution. |
| `alerting.notifier` | `none` for this local profile. |
| `validation.mode`, `sample_size` | `count`, `1000` as configured. Count validation is not exact-data comparison. |
| `logging.level` | `INFO`. |

Do not add PDBADMIN or SYS passwords to this YAML. A separate setup configuration is not required.

## 4. Run one-time PLAN and APPLY

Run from the repository root in PowerShell, after exiting SQL*Plus. No ENV password commands are required before the first interactive APPLY. The script collects required credentials through hidden prompts.

| Command | Purpose |
| --- | --- |
| `python -m migration_platform.oracle_setup_plan --config config/oracle_local_to_local.yaml` | Optional read-only review of requirements and proposed operations. |
| `python -m migration_platform.oracle_setup_plan --config config/oracle_local_to_local.yaml --apply` | Review and approve the plan; automatically provision the missing accounts, quotas and grants. |

### 4.1 First setup: run APPLY

Before starting, have these five credentials available: the two existing PDBADMIN passwords and three new passwords chosen for SOURCE_OWNER, TARGET_OWNER and TARGET_EXECUTOR (or their current passwords if the accounts already exist). Have authorized SYSDBA credentials available if the plan requires SYS-owned catalog grants. These are separate account credentials, even if the administrator username is the same on both PDBs.

```powershell
python -m migration_platform.oracle_setup_plan --config config/oracle_local_to_local.yaml --apply
```

Use the answers below for the example local environment. For a different installation, use its authorized accounts and current displayed values. Prompts are conditional: an existing account or available secret can change which questions appear. Do not type answers for prompts that are not shown.

#### A. Administrator credentials and existing accounts

| Terminal prompt | What to enter |
| --- | --- |
| `SOURCE PDB administrator username (blank to skip):` | `PDBADMIN` |
| `SOURCE PDB administrator password (hidden):` | Password chosen for SOURCE PDBADMIN in Section 2. |
| `TARGET PDB administrator username (blank to skip):` | `PDBADMIN` |
| `TARGET PDB administrator password (hidden):` | Password chosen for TARGET PDBADMIN in Section 2. |
| `Existing SOURCE owner password for secret oracle_local_source_owner_pass (hidden; Enter to skip):` | Current SOURCE_OWNER password if the account exists. |
| `Existing TARGET owner password for secret oracle_local_target_owner_pass (hidden; Enter to skip):` | Current TARGET_OWNER password if the account exists. |
| `Existing permission executor password for secret oracle_local_permission_executor_pass (hidden; Enter to skip):` | Current TARGET_EXECUTOR password if the account exists. |

For a fresh PDB pair, the three runtime accounts are absent; do not invent existing passwords. The three existing-account prompts are conditional, not required for confirmed absent accounts. Entering a blank at an optional inspection prompt may leave requirements unknown. Supply authorized credentials rather than skipping when they are needed to complete the plan.

#### B. Optional read-only SYSDBA inspection

These prompts appear only when catalog authority cannot be verified through the available accounts.

| Terminal prompt | What to enter |
| --- | --- |
| `SOURCE authorized SYSDBA username for read-only inspection (blank to skip):` | `SYS` in the tested environment, or another authorized SYSDBA for the SOURCE service. |
| `SOURCE SYSDBA password for read-only inspection (hidden):` | Matching authorized SYS password. |
| `TARGET authorized SYSDBA username for read-only inspection (blank to skip):` | `SYS` in the tested environment, or another authorized SYSDBA for the TARGET service. |
| `TARGET SYSDBA password for read-only inspection (hidden):` | Matching authorized SYS password. |

#### C. Runtime passwords for fresh accounts

| Terminal prompt | What to enter |
| --- | --- |
| `New SOURCE owner password for secret oracle_local_source_owner_pass (hidden):` | Choose a secure new password for SOURCE_OWNER. |
| `New TARGET owner password for secret oracle_local_target_owner_pass (hidden):` | Choose a secure new password for TARGET_OWNER. |
| `New permission executor password for secret oracle_local_permission_executor_pass (hidden):` | Choose a secure new password for TARGET_EXECUTOR. |

Store these three passwords securely; the same values will be entered for runtime ENV secrets in Section 6. If APPLY instead displays an `Existing ... password ... (hidden):` prompt, supply that account's current password. APPLY does not reset existing account passwords. Never enter passwords at username prompts.

#### D. Confirm endpoints, tablespace and quota

| Terminal prompt | What to enter |
| --- | --- |
| `Review SOURCE identity above; enter confirmation token <token>:` | Check SOURCE_DB_NEW / source_db_new and the expected owner or authorized inspection session, then type the exact token printed by this run. |
| `Review TARGET identity above; enter confirmation token <token>:` | Check TARGET_DB_NEW / target_db_new and the expected owner or authorized inspection session, then type the exact token printed by this run. |
| `Choose SOURCE permanent tablespace:` | `USERS` if offered and suitable; otherwise choose an approved displayed permanent tablespace. |
| `Choose TARGET permanent tablespace:` | `USERS` if offered and suitable; otherwise choose an approved displayed permanent tablespace. |
| `Press Enter to accept, or enter an override in MiB:` | Press Enter if the displayed finite recommendation is sufficient; otherwise enter an approved positive whole-MiB value. |

In the recorded local setup both owners used `USERS` with 49 MiB quota (recommendation based on 99 MiB free). The planner recommends half the reported free space rounded down to whole MiB. An override must be positive, finite and within reported free space. This is an example, not a fixed value. Existing suitable quotas are preserved. Tablespace and quota questions are skipped when no decision is needed. Review available storage against your actual data size.

#### E. Review and approve the plan

The planner lists account creation, quotas, system privileges, catalog access and any conditional administrative grants. Confirm that each operation is expected and that no unrelated database or principal is affected.

```text
PLAN STATUS: READY FOR REVIEW
PLAN FINGERPRINT: <current-fingerprint>
To apply exactly this plan, type APPLY <fingerprint>:
```

At the last prompt enter `APPLY ` followed by the exact fingerprint from the **current** plan. Do not type the angle brackets, reuse a historical fingerprint, or approve a blocked plan. This is interactive input, not a separate PowerShell command.

#### F. SYSDBA authorization after approval (only if required)

| Terminal prompt | What to enter |
| --- | --- |
| `SOURCE SYSDBA username (blank to cancel APPLY):` | `SYS` or an authorized SOURCE SYSDBA. |
| `SOURCE SYSDBA password (hidden):` | Matching SOURCE SYSDBA password. |
| `TARGET SYSDBA username (blank to cancel APPLY):` | `SYS` or an authorized TARGET SYSDBA. |
| `TARGET SYSDBA password (hidden):` | Matching TARGET SYSDBA password. |

These prompts authorize only the reviewed SYS-owned catalog grants; routine migration does not use SYSDBA. They do not appear on every APPLY.

### 4.2 Verify setup completion

```text
SETUP COMPLETE
Completed operations: <number>
```

The operation count depends on the current state. The recorded partial-recovery APPLY completed 25 operations after 11 earlier operations; do not use 25 as a fresh-setup requirement.

| Automatically configured | Recorded local result |
| --- | --- |
| SOURCE_OWNER | OPEN; USERS; finite 49 MiB quota; owner privileges and required source catalog access. |
| TARGET_OWNER | OPEN; USERS; finite 49 MiB quota; owner privileges and required target catalog access. |
| TARGET_EXECUTOR | READY; CREATE SESSION and required security catalog access. |

No manual CREATE USER, quota or repeated GRANT statements are needed for ordinary onboarding. Do not grant blanket DBA/SYSDBA to runtime accounts.

An optional repeat APPLY verifies idempotency. In the tested ready environment it returned:

```text
PLAN STATUS: NO CHANGES REQUIRED
NO CHANGES REQUIRED
```

On a no-change plan, no new quota, fingerprint approval or post-approval SYSDBA prompts are needed. Existing credentials and identity confirmation can still be requested. If the plan is BLOCKED or UNKNOWN, resolve the named requirement first. After a partial failure, replan; Oracle DDL is not rolled back as a group.

## 5. Prepare SOURCE and check conditional security authority

### 5.1 Prepare or import application objects

If SOURCE is already populated, skip creation and use the existing schema. For fresh local testing, create the application objects and insert data **after the initial APPLY**. Use the SOURCE owner connection for application DDL and DML. Use an authorized PDB administrator for application user/role administration if required.

The recorded Windows setup used SQL*Plus local OS authentication because its network SQL*Plus connection had returned ORA-12543. The following is that local DBA alternative, not a normal migration login. Only an authorized DBA should use it; `CURRENT_SCHEMA` changes name resolution, not the authenticated user.

In PowerShell:

```powershell
& "C:\app\admin\product\26ai\dbhomeFree\bin\sqlplus.exe" / as sysdba
```

At `SQL>`:

```sql
ALTER SESSION SET CONTAINER = SOURCE_DB_NEW;
ALTER SESSION SET CURRENT_SCHEMA = SOURCE_OWNER;
SELECT SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM DUAL;
```

Expected: `SOURCE_DB_NEW` and `SOURCE_OWNER`. The authenticated user remains SYS. Run the approved application SQL here if using this DBA alternative, then `COMMIT;` and `EXIT`. Never prepare test objects in TARGET. Use the complete test dataset in Document 05 once available; the uploaded runbook contains inventory evidence, not the complete original creation SQL transcript.

### 5.2 Conditional security setup after SOURCE preparation

If SOURCE includes application users, roles, system grants or role memberships, inspect the required executor authority with PLAN before the first FULL for that security inventory. This matters when initial APPLY was run against an empty SOURCE.

```powershell
python -m migration_platform.oracle_setup_plan --config config/oracle_local_to_local.yaml
```

If new supported, authorized operations are proposed, run the same APPLY command in Section 4.1 and approve the current plan. Enter existing account passwords, not new passwords. In the recorded populated-schema test the only additional operations were:

- Grant CREATE USER to TARGET_EXECUTOR.
- Grant CREATE ROLE to TARGET_EXECUTOR.
- Grant CREATE SESSION WITH ADMIN OPTION to TARGET_EXECUTOR.

APPLY completed 3 operations automatically. These are recorded operations, not SQL instructions for manual execution. Existing target roles may require separately reviewed role-specific administration authority. Ordinary supported SOURCE object grants do not require another APPLY if the required authority is already present. A new PowerShell terminal alone never requires APPLY.

### When to run APPLY again

| Situation | Action |
| --- | --- |
| New PDBs or missing migration accounts | Run APPLY. |
| New PowerShell terminal only | Set runtime ENV secrets; no APPLY. |
| Source data/table/view changes | Normal FULL if setup remains sufficient. |
| Supported SOURCE object grant added | Normal FULL if existing authority is sufficient. |
| New source security inventory requiring executor authority | Run read-only PLAN; APPLY only for reviewed missing authority. |
| Existing setup has no changes | Normal FULL; do not repeat APPLY. |
| Setup partially failed | Review current state and run a fresh APPLY for remaining approved operations. |

## 6. Run normal FULL migration

### 6.1 Set runtime ENV passwords in the current terminal

Set the three environment variables in the same PowerShell session used to run FULL. Enter the existing SOURCE_OWNER, TARGET_OWNER and TARGET_EXECUTOR passwords when prompted. These are the exact commands used in the local migration workflow.

```powershell
$env:SECRET_oracle_local_source_owner_pass = Read-Host "SOURCE password"
$env:SECRET_oracle_local_target_owner_pass = Read-Host "TARGET password"
$env:SECRET_oracle_local_permission_executor_pass = Read-Host "Executor password"
```

| PowerShell prompt | Enter this password |
| --- | --- |
| `SOURCE password` | Existing SOURCE_OWNER password selected during APPLY. |
| `TARGET password` | Existing TARGET_OWNER password selected during APPLY. |
| `Executor password` | Existing TARGET_EXECUTOR password selected during APPLY. |

Do not use PDBADMIN or SYS passwords for these three runtime secrets.

These variables are session-scoped. Set them again in a new PowerShell session or when credentials change. APPLY does not populate the parent PowerShell environment. Standard `Read-Host` displays typed input; enter passwords privately and do not share terminal recordings or screenshots containing them. Do not persist passwords using `setx` or commit them to the repository.

For hidden input, use these **instead of** the three simple commands, not in addition to them:

```powershell
$env:SECRET_oracle_local_source_owner_pass = [System.Net.NetworkCredential]::new('', (Read-Host "SOURCE password" -AsSecureString)).Password
$env:SECRET_oracle_local_target_owner_pass = [System.Net.NetworkCredential]::new('', (Read-Host "TARGET password" -AsSecureString)).Password
$env:SECRET_oracle_local_permission_executor_pass = [System.Net.NetworkCredential]::new('', (Read-Host "Executor password" -AsSecureString)).Password
```

### 6.2 Run FULL

Review the target scope before execution. Managed target rows and objects are reconciled to SOURCE. Independent target-only objects are preserved when safe; unsafe conflicts can block the run. FULL is not incremental UPSERT. Detailed reconciliation rules belong in Document 04.

```powershell
python -m migration_platform --config config/oracle_local_to_local.yaml --mode full
```

The normal command starts the report server after migration. Stop it with `Ctrl+C` after viewing the report. For automated acceptance testing, append `--exit-after-run` to exit without keeping the report server running. Do not run APPLY before every FULL.

### Check the report

| Check | Expected for the verified demo |
| --- | --- |
| Migration status | SUCCESS; zero reported errors |
| Tables and data | 5/5 tables; 50/50 rows; 100% |
| Managed object validation | managed_object_status_validation.status = verified |
| Report files | reports/<run_id>.json and .html; logs/<run_id>.jsonl |

The tested corrected FULL and repeat FULL completed in 196.2 s and 33.9 s respectively. Both JSON reports are documented as verified: 24 managed objects, 15 managed constraints and zero invalid managed objects or compilation errors. These timings are observations, not performance guarantees.

### On failure

Inspect the first error and the report; confirm TARGET state before retrying. Oracle DDL may already have committed. Do not drop application objects, disable safety checks or reapply stale setup plans as workarounds.

## 7. Verify and hand over

Connect to the intended SOURCE and TARGET owner sessions through the configured service (not CDB$ROOT). Run these read-only SQL statements on both owners and compare the managed inventory; internal constraint indexes and framework metadata can legitimately differ.

```sql
SELECT object_name, object_type, status
FROM user_objects WHERE generated = 'N'
ORDER BY object_type, object_name;

SELECT object_name, object_type, status
FROM user_objects WHERE status = 'INVALID';

SELECT name, type, line, text FROM user_errors
WHERE attribute = 'ERROR' ORDER BY name, type, sequence;
```

Confirm all SOURCE-managed objects exist and are valid, with no managed compilation errors. Compare row counts and the data/permission checks in Document 05. Independent target-only invalid objects are outside the managed migration gate; investigate them separately. A repeat FULL is an acceptance test, not a routine prerequisite.

### Acceptance record — 26 November 2026

| Evidence | Result |
| --- | --- |
| Corrected FULL | bfd97adbdcd14347a5108d8ef6792e72 — 50/50, zero errors |
| Corrected repeat FULL | f79b3095da5e406e92e75416a3f1fc8e — 50/50, zero errors |
| Independent owner comparison | 0 missing/mismatched objects, 0 invalid TARGET objects, 0 compilation errors |
| Unit / Oracle unit tests | 2,118 / 826 passed; Oracle Ruff scope passed |
| Integration / guarded E2E | 14 collected, not executed / 2 skipped |

Scope: tested local Oracle FULL workflow accepted. Cloud, all object variants, current-profile target-only preservation scenarios and unexecuted integration/E2E suites are not certified by this result.

### Troubleshooting essentials

| Symptom | Action |
| --- | --- |
| ORA-00922 / ORA-03048 on SQL*Plus paste | Use separate complete SQL statements; PL/SQL slash on its own line. |
| SQL*Plus ORA-12543 | Root cause unconfirmed in recorded setup; verify service and Python Thin login. |
| Missing ENV secret / YAML parse error | Restore exact SECRET_<reference> in this shell; use spaces for YAML. |
| Blocked setup or FULL | Inspect named authority/metadata blocker; use reviewed PLAN/APPLY only if new setup authority is needed. |

### Source notes

Consolidated from docs/oracle/ORACLE_LOCAL_TO_LOCAL_RUNBOOK.md, ORACLE_SETUP_PLAN.md, ORACLE_TERMINAL_REOPEN_RUNBOOK.md, ORACLE_FULL_MIGRATION.md, ORACLE_PERMISSION_MIGRATION.md and the supplied live acceptance record. The local SQL*Plus SOURCE preparation alternative is from the recorded project conversation. The current local profile and latest verified results take precedence over stale historical E2E endpoints and pending-status paragraphs. No live commands were executed to prepare this document.
