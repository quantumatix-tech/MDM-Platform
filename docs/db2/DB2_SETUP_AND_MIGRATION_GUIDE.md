# Db2 Migration Platform — Client Handover Guide

## 1. What Db2-to-Db2 Migration Supports

The Migration Platform migrates IBM Db2 LUW databases to IBM Db2 LUW databases. It supports:

- **Full migration mode** (schema + data + objects + grants)
- **Read-only preflight** and **acceptance verification**
- **Transactional data load** with identity restart safety
- **Fail-closed repeat FULL** (aborts if target tables contain data)

### Supported Object Types

Schemas, tables (ordinary + range-partitioned), columns/UDTs (distinct and structured), DEFAULT/generated columns, identity columns, standalone sequences, PK/FK/UNIQUE/CHECK constraints, indexes, views, MQTs, aliases, SQL functions, SQL procedures, triggers, table/column comments, grants, roles, role memberships.

### Not Supported

- CDC incremental mode (out of scope, intentionally excluded)
- Identity columns with INCREMENT BY 0 (not supported by code — restart computes MAX+0=MAX causing collision, verified by unit test; Db2 LUW rejection claim unverified)
- Cyclic foreign keys (detected and rejected by design)

### Synonyms

Db2 LUW does not have a separate SYNONYM object type. Db2 **aliases** serve the synonym use case and are fully supported. The EMP_SYNONYM alias was successfully migrated in acceptance run `33f9fadbeeb149bab34dc13980d0613c` (OBJSC01 → OBJTG08).

## 2. Prerequisites

### Software

| Component | Required | Tested |
|-----------|----------|--------|
| Python | 3.11+ | 3.12.6 |
| IBM Db2 LUW | 11.5+ | 11.5.9.0 |
| IBM DB2 ODBC DRIVER | Any DB2COPY variant | DB2COPY1 |
| pyodbc | >= 4.0 | 5.3.0 |

### Installation

```bash
pip install -e ".[dev]"
```

### Database Creation (Client Responsibility)

Create source and target databases before migration. These are Db2 Command Window (CMD) commands:

```cmd
REM Create source database
db2 create database <SOURCE_DB>

REM Create target database
db2 create database <TARGET_DB>
```

After creating each database, connect to it before granting privileges:

```cmd
db2 connect to <TARGET_DB>
```

These grant commands were verified for isolated OBJTG08 acceptance. Properly authorized database administration is required. A fresh target requires appropriate grants again.

## 3. Minimum Privileges

### Source (read-only discovery)

```sql
GRANT CONNECT ON DATABASE TO USER <user>;
GRANT SELECTIN ON SCHEMA <schema> TO USER <user>;
GRANT SELECT ON TABLE <schema>.<table> TO USER <user>;
-- Plus implicit read access to SYSCAT views (granted to PUBLIC by default)
```

**Important**: `SELECTIN ON SCHEMA` grants schema-level select permission. For table-level access, use `GRANT SELECT ON TABLE <schema>.<table> TO USER <user>`. These are distinct privileges in Db2 11.5; verify the exact syntax against IBM Db2 LUW 11.5 documentation for your environment.

### Target (DDL + data + grants) — Verified in E2E

```sql
GRANT DBADM ON DATABASE TO USER <user>;
GRANT SECADM ON DATABASE TO USER <user>;
GRANT DATAACCESS ON DATABASE TO USER <user>;
GRANT ACCESSCTRL ON DATABASE TO USER <user>;
GRANT CREATE_EXTERNAL_ROUTINE ON DATABASE TO USER <user>;
```

**Note**: DBADM is a broad convenience privilege used in testing. Proposed least-privilege alternatives (CREATEIN, SELECT/INSERT/UPDATE/DELETE, USAGE, EXECUTE) are **not verified**. Broad DBADM/SECADM is not an automatically approved production least-privilege policy. Do not use in production without testing.

### Security Executor (for role self-grants, avoids SQL0554N)

The security executor is a separate Db2 connection used exclusively for role creation and GRANT ROLE operations. It must hold SECADM authority on the target database.

```sql
-- On the target database
GRANT SECADM ON DATABASE TO USER <admin_user>;
GRANT DBADM ON DATABASE TO USER <admin_user>;
```

The security executor must be a **different principal** from the migration user (DB2ADMIN). If the migration user attempts to GRANT a role to itself, Db2 raises SQL0554N ("authorization ID cannot grant privileges to itself"). The security executor avoids this because SECADM can grant to any principal.

### Why SQL0554N Occurs

Db2 prohibits a principal from granting privileges (including role membership) to itself. When the migration user (e.g. DB2ADMIN) is also the grantee of a role membership discovered on the source, a direct `GRANT ROLE ... TO DB2ADMIN` from the migration connection fails with SQL0554N. The security executor (a separate SECADM-held connection) performs the grant instead.

SQL0554N self-grants are executed through the distinct security executor connection, not the migration connection.

### Why ADMIN and DB2ADMIN Passwords Are Separate

DB2ADMIN is the normal migration user for data and object migration. ADMIN is a separate SECADM-held principal used only for role operations. They are separate database users with separate passwords, configured via separate `password_secret` environment variables.

DB2ADMIN and the separate ADMIN security executor have different responsibilities and credentials:
- **DB2ADMIN**: migration user for schema, data, and object migration (DBADM, DATAACCESS, ACCESSCTRL)
- **ADMIN**: security executor for role creation and role membership grants (SECADM, DBADM)

### Password Secret Configuration (No Plaintext Passwords)

Configure source, target and security-executor password secrets as environment variables. **Never hardcode passwords in YAML files or scripts.** Use `Read-Host -AsSecureString` on Windows PowerShell to avoid displaying secrets on screen:

```powershell
# Source connection secret
$env:SECRET_db2_source_pass = (Read-Host -AsSecureString -Prompt "Source DB2ADMIN password")

# Target connection secret (migration user)
$env:SECRET_db2_target_pass = (Read-Host -AsSecureString -Prompt "Target DB2ADMIN password")

# Security executor secret (role operations)
$env:SECRET_db2_admin_pass = (Read-Host -AsSecureString -Prompt "Target ADMIN security executor password")
```

The three secret names used in local acceptance are:
- `SECRET_db2_source_pass` — source database password
- `SECRET_db2_target_pass` — target database migration user password (DB2ADMIN)
- `SECRET_db2_admin_pass` — security executor password (ADMIN, SECADM)

Keep all secrets outside YAML. In YAML, reference only the env var name (without `SECRET_` prefix):

```yaml
password_secret: db2_source_pass        # under source.connection
password_secret: db2_target_pass        # under target.connection
password_secret: db2_admin_pass         # under security_executor.connection
```

## 4. Configuration

Copy the template:

```bash
cp config/db2.example.yaml config/db2_client.yaml
```

Edit all `<placeholder>` values. Set secrets using the PowerShell examples in Section 3.

### YAML Field Reference

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `source.engine` | Yes | — | Must be `db2` |
| `source.connection.host` | Yes | — | Source hostname/IP |
| `source.connection.port` | No | 25000 | Source port |
| `source.connection.database` | Yes | — | Source database name |
| `source.connection.username` | Yes | — | Source user |
| `source.connection.password_secret` | Yes | — | Env var name (without SECRET_ prefix) |
| `source.connection.driver` | Yes | — | ODBC driver name |
| `target.engine` | Yes | — | Must be `db2` |
| `target.connection.*` | Yes | — | Same as source fields |
| `target.connection.source_engine` | Yes | — | Must be `db2` |
| `security_executor.engine` | No | — | Must be `db2` when configured |
| `security_executor.connection.*` | No | — | Same as target fields (separate SECADM user) |
| `security_executor.connection.password_secret` | No | — | Env var name for admin user |
| `migration.mode` | Yes | — | Must be `full` |
| `migration.batch_size` | No | 1000 | Rows per batch |
| `migration.include_schemas` | Yes | — | List of schemas |
| `secrets.provider` | Yes | — | Must be `env` |
| `validation.mode` | No | count | Validation mode |
| `validation.sample_size` | No | 100 | Sample size |
| `validation.strict_grant_parity` | No | false | Fail on grant differences |
| `alerting.notifier` | No | none | Alert notifier |
| `logging.file` | No | logs/db2_client.log | Log file path |
| `logging.level` | No | INFO | Log level |

## 5. Preflight (Read-Only)

```bash
python scripts/db2/verify_db2_e2e_preflight.py --config config/db2_client.yaml
```

Verifies: connectivity, schema existence, target emptiness, object counts, UDTs, aliases, target privileges.

## 6. FULL Migration

```bash
python -m migration_platform --config config/db2_client.yaml --mode full --no-live-ui
```

### Fresh-Target Requirement

A fresh empty target is required for every FULL migration run. The fail-closed guard runs **before any target mutation** and aborts if:

- Existing target tables (including zero-row tables) in scope are detected
- Pre-existing scoped structured UDTs on target are detected
- Pre-existing scoped aliases on target are detected
- Pre-existing scoped custom roles on target (excluding Db2 SYS* roles) are detected

Partition preflight runs before any target migration; mismatch blocks migration.

Creating another fresh target database requires repeating its appropriate database grants (see Section 3).

### Transaction Behavior

- **Role DDL** (CREATE ROLE, GRANT ROLE) runs in a single atomic transaction on the security executor connection (SECADM). It is committed independently of the data-load transaction via `executor_conn.commit()`. On any failure, the role transaction is rolled back via `executor_conn.rollback()`.
- **Data load** (DELETE/INSERT) shares one transaction on `target._conn` with `autocommit=False`; commit occurs once after count validation via `conn.commit()`. On any failure, `conn.rollback()` — the entire data load is atomic.
- **DDL outside transaction** (indexes, constraints, FKs, sequences, views, MQTs, aliases, triggers, routines, comments, defaults, checks) runs after the data-load commit with `autocommit=True`; failures are recorded as non-fatal and may persist.
- `target._conn` is never committed early for role work.

### Transaction Visibility and Limitations

- Once the role transaction commits on the security executor connection, a later rollback of another connection cannot undo it. Committed role definitions and memberships become durable and visible to all connections.
- Once the data-load transaction commits on `target._conn`, the committed data is durable. A later rollback of another connection cannot undo it.
- A later failure (e.g. post-commit DDL error) may leave partial committed objects on the target.
- There is **no guaranteed global atomicity** across the two connections (security executor and target) without distributed transaction support. The two transactions commit independently.
- Failed or partially modified targets must not be re-run blindly. Use a fresh empty target for recovery.

## 7. Acceptance Verification

```bash
python scripts/db2/verify_db2_acceptance.py --config config/db2_client.yaml
```

### Acceptance Gates

| Gate | Pass Criteria |
|------|---------------|
| DEFAULT parity | 0 mismatches |
| Triggers | All VALID=Y, ENABLED=Y |
| Identity | RESTART WITH MAX+increment (no collision) |
| Data | All source rows in target |
| Objects | All types match |
| Partitions | Boundaries match |
| Constraints | PK/FK/CHECK/UNIQUE match |
| Errors | 0 |

### Expected Warnings (Non-Blocking)

- `IDENTITY_RUNTIME`: NEXTCACHE differs (cache state, not collision risk)
- `GRANT_OWNERSHIP`: Ownership-generated privileges (informational)
- `GRANT_OPTION`: Grantable flag differences
- `GRANT_EXCESS`: Non-ownership excess (review for security)

## 8. Empty-Target Protection and Repeat-FULL Restrictions

The fail-closed guard runs **before any target mutation** and detects:

- Existing target tables (including zero-row tables) in scope → repeat FULL is blocked
- Pre-existing scoped structured UDTs on target → blocked
- Pre-existing scoped aliases on target → blocked
- Pre-existing scoped custom roles on target (excluding Db2 SYS* roles) → blocked

Tolerates SQL0204N ("object does not exist") for fresh targets; fail-closes on other errors.

Partition preflight runs before any target mutation; mismatch blocks migration.

A fresh empty target is required for every FULL migration run. Creating another fresh target database requires repeating its appropriate database grants (see Section 3).

## 9. Transaction Visibility, Commit and Rollback

### Role Transaction (Security Executor)

- All role DDL runs on the security executor connection
- The executor holds SECADM, so it can create any role and grant any membership (including self-grants)
- The role transaction is committed independently of `target._conn`
- On success: `executor_conn.commit()` — role definitions and memberships become durable and visible to all connections
- On failure: `executor_conn.rollback()` — no orphaned roles or partial memberships
- Committed grants survive executor connection close

### Data-Load Transaction (Target Connection)

- DELETE/INSERT share one transaction with `autocommit=False`
- Commit occurs once after count validation (line 519 of `db2_full.py`)
- On any failure: `conn.rollback()` — entire data load is atomic
- `target._conn` is restored to its original autocommit setting in the `finally` block

### Cleanup

- Security executor connection and connector are closed in the `finally` block
- `target._conn` autocommit is restored to its original value

## 10. Reports and Recovery

Reports are generated in `reports/`:
- HTML: `reports/<run_id>.html`
- JSON: `reports/<run_id>.json`
- Manifest: `reports/<run_id>_manifest.json`

### Failure Recovery

- **Partial failure**: Use a fresh empty target (do not re-run on populated target)
- **Object errors**: Check `_ddl_errors` in report JSON
- **SQLSTATE diagnostics**: Captured in exception messages
- **Identity collision**: Prevented by RESTART WITH MAX+increment

## 11. Client Onboarding Checklist

1. **Install prerequisites**: Python 3.11+, IBM Db2 LUW 11.5+, IBM DB2 ODBC DRIVER
2. **Install platform**: `pip install -e ".[dev]"`
3. **Create databases**: Source and empty target (client responsibility, Db2 CMD commands)
4. **Connect to target**: `db2 connect to <TARGET_DB>`
5. **Grant privileges**: DBADM/SECADM on target; CONNECT/SELECTIN/SELECT on source
6. **Grant SECADM to security executor user** (for role self-grants)
7. **Copy config**: `cp config/db2.example.yaml config/db2_client.yaml`
8. **Edit config**: Fill in host, port, database, username, schema; add `security_executor` section if using roles
9. **Set secrets**: `$env:SECRET_db2_source_pass`, `$env:SECRET_db2_target_pass`, `$env:SECRET_db2_admin_pass` (use `Read-Host -AsSecureString`)
10. **Run preflight**: `python scripts/db2/verify_db2_e2e_preflight.py --config config/db2_client.yaml`
11. **Run FULL**: `python -m migration_platform --config config/db2_client.yaml --mode full --no-live-ui`
12. **Run acceptance**: `python scripts/db2/verify_db2_acceptance.py --config config/db2_client.yaml`
13. **Review warnings**: Classify grant-option differences (see Technical Reference)
14. **Archive reports**: Save HTML/JSON reports from `reports/` directory

## 12. Known Limitations

1. **CDC incremental**: Not implemented; out of scope for this release
2. **Increment=0 identity**: Not supported by code — restart computes MAX+0=MAX causing collision (verified by unit test). Db2 LUW rejection claim unverified.
3. **Cyclic FKs**: Not supported; detected and rejected by fail-closed design
4. **Composite FKs**: Discovery and DDL generation exist via SYSCAT.KEYCOLUSE; multi-column FK ordering unit-tested, but no live multi-column FK E2E evidence (CODE PRESENT / LIVE E2E NOT VERIFIED).
5. **Repeat FULL**: Blocked on populated targets (fail-closed); fresh target required
6. **Least-privilege setup**: Only DBADM/SECADM tested; proposed minimums not verified
7. **Global atomicity**: No guaranteed global atomicity across security executor and target connections without distributed transaction support
8. **Synonyms**: Not a separate Db2 object type; aliases cover this use case (supported)

---

**Evidence**: Run `33f9fadbeeb149bab34dc13980d0613c` (OBJSC01 → OBJTG08, 1 table, 2/2 rows, 0 errors, 0 mismatches, unit tests 1269 passed)

**Previous runs**: Run `b93dc7dbc29b479ab1561953b82b07fe` (E2ESRC02 → E2ETGT07, 9 tables, 22 rows, 0 mismatches, 0 errors, report JSON status="success"); Run `96980b310bca4d88ab6bc04b101b1f2c` (E2ESRC02 → E2ETGT07, 9 tables, 22 rows, 0 mismatches, 0 errors, report JSON status="success"); Run `30f998eb2f9e4827b539d76dfdc30f49` (E2ESRC02 → E2ETGT07, 9 tables, 22 rows — NO report file found; status unverified)
