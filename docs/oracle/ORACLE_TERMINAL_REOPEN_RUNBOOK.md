# Oracle Terminal Reopen Runbook

This runbook resumes work after closing PowerShell, SQL*Plus, VS Code, or
restarting Windows. It describes the checked-in Oracle configuration and
commands; it does not store credentials or run anything automatically.

## Current configured endpoint references

The current local acceptance YAML is `config/oracle_e2e_acceptance.yaml`:

| Side / purpose | Service | User | Secret reference |
|---|---|---|---|
| SOURCE owner | `ORACLE_SOURCE_DB` | `MIGE2E_SIMPLE_SRC` | `oracle_e2e_source_pass` |
| TARGET owner | `ORACLE_TARGET_DB` | `MIGE2E_SIMPLE_TGT` | `oracle_e2e_target_pass` |
| Permission executor | TARGET service | `ORACLE_PERMISSION_EXECUTOR` | `oracle_permission_executor_password` |
| TARGET privilege-bootstrap admin | TARGET service | `ORACLE_TARGET_ADMIN` | `oracle_e2e_target_admin_pass` |
| Optional SOURCE onboarding admin | SOURCE service | `ORACLE_SOURCE_ADMIN` | `oracle_e2e_source_admin_pass` |

The configured CDB unique name and local SQL*Plus SID are `FREE`; the network
services are the two PDB names above. Confirm these against YAML before use.
Never substitute another PDB. The Oracle Python connector uses
`python-oracledb` Thin mode by default. It does not call
`oracledb.init_oracle_client()` and does not require Instant Client for normal
FULL migration. SQL*Plus is a separate executable needed only by local DBA
bootstrap/discovery commands.

## 1. Open a new terminal and enter the repository

**REQUIRED EACH NEW TERMINAL — local shell only; no database access.**

```powershell
Set-Location D:\dmt_migration\MDM-Platform
Get-Location
```

The migration CLI resolves relative config, report, and log paths from the
current directory. Run commands in this runbook from the repository root.

## 2. Activate or prepare the Python environment

At inspection time the repository had no `.venv` directory. Python 3.11 or
newer is required by `pyproject.toml`. The checked-in project instructions use
a local virtual environment and pip; they do not require uv. `uv.lock` exists,
but this runbook uses the documented pip setup.

**REQUIRED EACH NEW TERMINAL, if `.venv` exists — local shell only.**

```powershell
if (-not (Test-Path .\.venv\Scripts\Activate.ps1)) { throw 'Project .venv is missing; follow ONE-TIME INITIAL SETUP below.' }
. .\.venv\Scripts\Activate.ps1
```

**ONE-TIME INITIAL SETUP — creates a venv and installs packages; no database access.**

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m migration_platform --version
```

Do not run installation commands on each reopen. If using a separately managed
Python environment, activate it instead and verify it below.

## 3. Check Python, dependencies, and SQL*Plus

**READ-ONLY — local interpreter/module and executable checks only.**

```powershell
python --version
python -c "import oracledb, yaml; print('oracledb', oracledb.__version__); print('Thin mode', oracledb.is_thin_mode()); print('PyYAML', yaml.__version__)"
Get-Command sqlplus -ErrorAction SilentlyContinue | Select-Object Name,Source
if (Get-Command sqlplus -ErrorAction SilentlyContinue) { sqlplus -v }
```

Expected: Python 3.11+, importable `oracledb` and `yaml`, and Thin mode `True`.
Normal FULL uses the Python driver and does not need SQL*Plus. Optional local
SYSDBA onboarding requires SQL*Plus, a working local Oracle home, `ORACLE_SID`,
and the matching Windows Oracle service. The repository's `bootstrap.py`
driver list does not include Oracle; use the import check above for Oracle.

## 4. Restore credentials without printing them

The YAML stores bare secret references. `EnvSecretProvider` reads the exact
process variable `SECRET_<reference>`; for example,
`password_secret: oracle_e2e_source_pass` reads
`SECRET_oracle_e2e_source_pass`. A PowerShell `$env:` assignment is scoped to
that process and its children. It disappears when that terminal closes and is
not restored by YAML or by starting a new VS Code terminal.

The current canonical YAML sets `secrets.provider: env`. The permission
executor has one documented fallback: on a missing environment lookup, the
migration orchestrator tries the existing Windows Local Encrypted Store. The
default store is under `%LOCALAPPDATA%\MigrationPlatform\secrets.enc`, with
its encryption key in Windows Credential Manager. This fallback applies to
the executor only; it does not load SOURCE/TARGET owner or admin passwords.

**OPTIONAL, READ-ONLY — verifies encrypted executor secret without displaying it.**

```powershell
python -m migration_platform.secrets_cli verify oracle_permission_executor_password
```

Do not put passwords in YAML, source files, command arguments, or a PowerShell
profile. If a secret is not available in the configured provider, restore it
for this terminal with hidden prompts:

**REQUIRED EACH NEW TERMINAL for the current config, unless the executor secret is resolved from its encrypted store — process-only credential loading.**

```powershell
function Set-OracleProcessSecretFromPrompt {
    param([Parameter(Mandatory)][string]$EnvironmentName, [Parameter(Mandatory)][string]$Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        [Environment]::SetEnvironmentVariable($EnvironmentName, $plain, 'Process')
        $plain = $null
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        Remove-Variable secure,pointer,plain -ErrorAction SilentlyContinue
    }
}
Set-OracleProcessSecretFromPrompt 'SECRET_oracle_e2e_source_pass' 'SOURCE owner password'
Set-OracleProcessSecretFromPrompt 'SECRET_oracle_e2e_target_pass' 'TARGET owner password'
Set-OracleProcessSecretFromPrompt 'SECRET_oracle_permission_executor_password' 'Permission executor password'
```

The driver needs the password in process memory. The prompt avoids echoing it
and the command line/history contains no value. Clear process variables when
finished:

**OPTIONAL — removes only this process's credential variables.**

```powershell
Remove-Item Env:SECRET_oracle_e2e_source_pass,Env:SECRET_oracle_e2e_target_pass,Env:SECRET_oracle_permission_executor_password,Env:SECRET_oracle_e2e_target_admin_pass,Env:SECRET_oracle_e2e_source_admin_pass -ErrorAction SilentlyContinue
```

Normal FULL requires SOURCE and TARGET owner credentials. Because the canonical
config enables permission migration, it also requires the executor credential
and a usable TARGET-local executor. The target admin secret is needed only if
target owner DDL privileges are missing and the configured bootstrap must grant
them. SOURCE/TARGET provisioning-admin credentials are for fresh E2E
onboarding, not normal FULL. Do not prompt for unused admin secrets routinely.

For optional onboarding or an authorized TARGET grant, restore the relevant
admin secret only when the reviewed plan requires it:

**OPTIONAL, DATABASE MUTATION PATH — prompt only; does not itself connect or grant.**

```powershell
Set-OracleProcessSecretFromPrompt 'SECRET_oracle_e2e_target_admin_pass' 'TARGET PDB-local admin password'
```

**OPTIONAL, FRESH E2E DBA ONBOARDING ONLY — not for normal FULL.**

```powershell
Set-OracleProcessSecretFromPrompt 'SECRET_oracle_e2e_source_admin_pass' 'SOURCE PDB-local admin password'
```

Never persist these values using user- or machine-scoped environment variables;
Windows environment-variable storage is not the configured encrypted secret
provider. Other vault providers are supported by the shared provider factory,
but changing the current YAML provider is a separate configuration decision.

## 5. Check which references are available

**READ-ONLY — prints reference names and booleans only, never secret values.**

```powershell
$secretRefs = @('oracle_e2e_source_pass','oracle_e2e_target_pass','oracle_permission_executor_password','oracle_e2e_target_admin_pass','oracle_e2e_source_admin_pass')
foreach ($ref in $secretRefs) { $name = "SECRET_$ref"; [pscustomobject]@{ Reference=$ref; ProcessEnvironmentAvailable=[bool][Environment]::GetEnvironmentVariable($name,'Process') } }
```

`False` for owner or executor references means this terminal cannot resolve
them from `env`; restore them as above or verify the executor encrypted store.
An available variable does not prove its password is correct; use the
read-only owner identity check next.

## 6. Verify populated SOURCE/TARGET owner authentication and identity

Use this for existing populated owners. It resolves configured owner
credentials and checks username, service, PDB, and configured CDB identity
without requiring empty schemas.

**READ-ONLY — no DDL, fixtures, or row changes.**

```powershell
python -m tests.e2e.oracle.check_metadata_compatibility --config config/oracle_e2e_acceptance.yaml --auth-only
```

For full metadata discovery, permission-executor authentication/catalog
checks, and a summarized read-only TARGET inventory, use:

**READ-ONLY database inspection; writes a new sanitized details JSON file.**

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
python -m tests.e2e.oracle.check_metadata_compatibility --config config/oracle_e2e_acceptance.yaml --details-file "reports/oracle_metadata_$stamp.json"
```

The compatibility checker reports `UNVERIFIED` if TARGET inventory cannot be
read. Do not treat failed metadata visibility as an empty target. Its detail
file uses exclusive-create semantics; choose a new timestamp if the path exists.

## 7. Use the correct populated-schema read-only planner

**READ-ONLY — inspects the configured Oracle FULL replacement plan and exits.**

```powershell
python -m migration_platform --config config/oracle_e2e_acceptance.yaml --mode full --oracle-overwrite-plan
```

Review the full plan, `inspection_errors`, blockers, managed scope, and any
destructive DDL before approving FULL. This planner is suitable for populated
TARGETs. It is not the fresh-owner fixture preflight.

**Do not use these for ordinary populated-schema verification:**
`python -m tests.e2e.oracle.run_preflight` checks fixture prerequisites; it
requires an empty TARGET and, in `source_mode: fixtures`, an empty SOURCE.
Likewise, `scripts/oracle_live_e2e.ps1 -PreflightOnly` builds the fresh E2E
onboarding plan and stops when the existing SOURCE owner is not empty. That is
expected, not evidence of an authentication or migration defect.

## 8. Run normal FULL only after explicit authorization

FULL replaces rows only in the source-managed target table scope. It can also
reconcile supported source-managed schema and objects. Oracle DDL commits
implicitly, so review the read-only plan and current target inventory first.

**DATABASE MUTATION / REQUIRES APPROVAL — performs actual FULL migration.**

```powershell
python -m migration_platform --config config/oracle_e2e_acceptance.yaml --mode full --no-live-ui --exit-after-run
```

The CLI's default mode starts a report server bound to `0.0.0.0` and keeps the
process open until interrupted. `--no-live-ui --exit-after-run` avoids the
Rich UI and exits after writing reports. Do not migrate when the plan is
blocked, identity is unexpected, catalog access is incomplete, or target
inventory is unverified.

## 9. Run independent validation

**READ-ONLY database checks; writes a validation JSON report.**

```powershell
python -m tests.e2e.oracle.run_validation --config config/oracle_e2e_acceptance.yaml
```

This validator compares tables, expected rows, structures, keys, indexes,
supported object presence/definitions, sequence state, and configured checks.
It does not migrate rows. The current config has fixture expectations; use this
validator only when that expected table scope matches the owners. For separate
production schemas, use expectations appropriate to that schema.

## 10. Find reports and stop/reopen behavior

Migration reports are written under `reports\` as `<run_id>.html` and
`<run_id>.json`; audit events are under `logs\<run_id>.jsonl`. The CLI prints
the run ID and paths. Oracle E2E validation reports are under
`tests\e2e\oracle\reports\`. These files remain after terminal closure. The
live report/status server does not survive process termination; do not rerun
FULL merely to reopen a report.

**READ-ONLY — list recent report artifacts.**

```powershell
Get-ChildItem .\reports -File | Sort-Object LastWriteTime -Descending | Select-Object -First 10 Name,Length,LastWriteTime
Get-ChildItem .\logs -Filter '*.jsonl' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 10 Name,Length,LastWriteTime
```

## 11. SQL*Plus reconnect and PDB identity checks

For ordinary owner checks, connect directly to each configured service. SQL*Plus
prompts for the password; do not include it in the command.

**READ-ONLY — owner connection.**

```powershell
sqlplus MIGE2E_SIMPLE_SRC@//127.0.0.1:1521/ORACLE_SOURCE_DB
```

At the SQL*Plus prompt, each statement is one line:

**READ-ONLY SQL — each line is a single statement.**

```sql
SELECT USER, SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME'), SYS_CONTEXT('USERENV','DB_UNIQUE_NAME') FROM dual;
SELECT object_name, object_type, status FROM user_objects ORDER BY object_type, object_name;
EXIT
```

For TARGET, connect with
`MIGE2E_SIMPLE_TGT@//127.0.0.1:1521/ORACLE_TARGET_DB` and run the same
identity/object queries. Use the actual host, port, username, and service from
the selected config if it differs.

Local SYSDBA CDB switching is for an authorized DBA session, not ordinary
migration. The onboarding profile specifies local SID `FREE`. Setting
`ORACLE_SID` below changes only this PowerShell process; it does not alter a
Windows service or database setting.

**OPTIONAL, READ-ONLY — local SYSDBA identity and PDB switching; requires local SYSDBA authorization.**

```powershell
$env:ORACLE_SID = 'FREE'
sqlplus / as sysdba
```

At SQL*Plus, switch and verify only the configured PDBs. `ALTER SESSION` below
changes session context only.

**READ-ONLY SQL/session context — no DDL or data changes.**

```sql
SELECT SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), SYS_CONTEXT('USERENV','CON_NAME') FROM dual;
ALTER SESSION SET CONTAINER = ORACLE_SOURCE_DB;
SELECT SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual;
ALTER SESSION SET CONTAINER = ORACLE_TARGET_DB;
SELECT SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), SYS_CONTEXT('USERENV','CON_NAME'), SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual;
EXIT
```

No multi-line PL/SQL is required here. If a PL/SQL block is used later, put `/`
on its own line as the SQL*Plus terminator.

## 12. Troubleshooting

| Symptom | Safe next check |
|---|---|
| Missing secret | Compare YAML references with the name-only environment check. Restore it for this process or verify the executor encrypted store. Never print the value. |
| ORA-01017 | Check username, corresponding secret reference, and configured service separately. Owner and admin passwords are distinct. Do not reset a password based only on this error. |
| ORA-00942 | Identify the exact catalog view and connected user. Missing visibility is not an empty inventory. Request only exact approved view access; never add `DBA` or `SELECT ANY DICTIONARY` as a workaround. |
| Wrong PDB/service | Stop before mutation. Compare `CON_NAME`, `SERVICE_NAME`, `DB_UNIQUE_NAME`, and YAML. Never fall back to another PDB. |
| `Existing SOURCE owner is not empty` | The fixture path requires an empty SOURCE. Skip fixture setup and use the metadata checker plus `--oracle-overwrite-plan`; do not delete objects to pass the gate. |
| Existing TARGET owner not empty | Expected for normal repeat FULL. Review its populated-schema plan; do not run fixture preflight or cleanup. |
| SQL*Plus unavailable | Normal FULL does not need SQL*Plus. For DBA onboarding only, use an authorized Oracle client and verify the local CDB/SID before proceeding. |
| Terminal/process closed during FULL | Inspect run JSON, JSONL audit, and current TARGET inventory before retry. Oracle DDL and committed batches may remain after failure. |

## 13. Starting a new Codex/ChatGPT session

Ask the new session to read these files before acting:

- `docs/oracle/ORACLE_TERMINAL_REOPEN_RUNBOOK.md`
- `docs/oracle/ORACLE_LOCAL_TO_LOCAL_RUNBOOK.md`
- `config/oracle_e2e_acceptance.yaml`

Ask it to preserve the worktree, distinguish populated-schema checks from
fresh fixture onboarding, and stop before database mutation unless you
explicitly authorize that specific run. A new session must recheck the current
YAML and environment; this runbook does not prove current connectivity or
credential availability.
