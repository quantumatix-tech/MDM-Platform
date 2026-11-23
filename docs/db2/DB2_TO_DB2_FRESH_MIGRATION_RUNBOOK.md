# IBM Db2-to-Db2 Fresh Migration — Local E2E Runbook

**Purpose:** Reproduce a clean, end-to-end Db2-to-Db2 migration from a newly created source database to a newly created target database, then independently verify the data and table metadata.

**Scope:** Local IBM Db2 LUW 11.5.x test environment; one schema, one employee table, 50 rows covering basic types, `DATE`, `TIME`, `TIMESTAMP(6)`, fractional seconds, and `NULL` values. This is an **E2E test procedure**, not a production permission model or an automatic recovery procedure.

**Expected Result:** A successful FULL migration should transfer all source records to the target without errors. Verify row counts, column datatypes, primary keys, and exact data values, including DATE, TIME, TIMESTAMP(6), microseconds, and NULLs.

## 0. Choose names and check your environment

The names and paths below are **examples**. Replace them for each new practice run; do not assume they match another machine.

| Setting | Example for the next run | Change when needed |
|---|---|---|
| Source database | `DBSRC2` | New, unused Db2 database name |
| Target database | `DBTG2` | New, unused Db2 database name |
| Source schema | `TEST_SCHEMA` | Schema to migrate |
| Table | `EMPLOYEE_DATA` | Test table |
| Migration login | `DB2ADMIN` | Db2 user used in the YAML connections |
| Administrative login | `ADMIN` | Account authorized to create databases and grant privileges |
| Config file | `config/db2_test2.yaml` | Local YAML file under the repository root |
| ODBC driver | `IBM DB2 ODBC DRIVER - DB2COPY1` | Exact name installed on your machine |
| Db2 host and port | `127.0.0.1:25000` | Actual reachable Db2 endpoint |
| Python project directory | `<repo-root>` | Directory containing `migration_platform` and `config` |

**Prerequisites:** Db2 services are available; Db2 Command Line Processor (CLP) commands work; source/target database names are unused; the intended administrative account has the appropriate authority; the selected ODBC driver and Python dependencies are installed. Run database commands from an initialized **Db2 Command Window / Db2 CLP environment**. For example, a local installation might have `C:\Program Files\IBM\SQLLIB\BIN`, but that path is **not required** if `db2` is on the command path.

**Safety:** Do not run `CREATE DATABASE`, `DROP TABLE`, or FULL migration against a database containing data you must preserve. All privileges below are for a controlled **local E2E test**, not an approved production least-privilege baseline. Never commit passwords or local secret-bearing configs.

## 1. Create the fresh SOURCE database and schema

**Terminal: Db2 Command Window (CMD), using the authorized administrative account.**

```cmd
db2 terminate
db2 create database DBSRC2
db2 connect to DBSRC2
db2 "SELECT CURRENT SERVER AS DB_NAME, SESSION_USER AS LOGIN_USER FROM SYSIBM.SYSDUMMY1"
db2 "CREATE SCHEMA TEST_SCHEMA"
```

Expect `DB20000I` for successful create commands. Confirm `DB_NAME = DBSRC2` before creating objects. The command to connect is **`db2 connect to DBSRC2`**, not `db2 connect DBSRC2`.

## 2. Create the SOURCE test table

```cmd
db2 "CREATE TABLE TEST_SCHEMA.EMPLOYEE_DATA (EMP_ID INTEGER NOT NULL PRIMARY KEY, EMP_NAME VARCHAR(60), DEPARTMENT CHAR(10), SALARY DECIMAL(10,2), IS_ACTIVE SMALLINT, JOIN_DATE DATE, LOGIN_TIME TIME, CREATED_AT TIMESTAMP(6), UPDATED_AT TIMESTAMP(6), REMARKS VARCHAR(100))"
```

Columns: `EMP_ID` (INTEGER, primary key), `EMP_NAME` (VARCHAR), `DEPARTMENT` (CHAR), `SALARY` (DECIMAL), `IS_ACTIVE` (SMALLINT), `JOIN_DATE` (DATE), `LOGIN_TIME` (TIME), `CREATED_AT` and `UPDATED_AT` (TIMESTAMP with six fractional digits), and `REMARKS` (VARCHAR).

## 3. Insert 50 representative SOURCE rows

The following single-line CMD command is the **same type of fixture that succeeded in the previous local E2E run**. It generates 50 employees and includes leap-day, year-end, midnight, fractional-second timestamp, and `NULL` cases.

```cmd
db2 "INSERT INTO TEST_SCHEMA.EMPLOYEE_DATA (EMP_ID, EMP_NAME, DEPARTMENT, SALARY, IS_ACTIVE, JOIN_DATE, LOGIN_TIME, CREATED_AT, UPDATED_AT, REMARKS) SELECT ID, 'Employee_' || TRIM(CHAR(ID)), CASE MOD(ID,5) WHEN 0 THEN 'IT' WHEN 1 THEN 'HR' WHEN 2 THEN 'FINANCE' WHEN 3 THEN 'SALES' ELSE 'OPS' END, DECIMAL(30000 + ID * 175.25,10,2), CASE MOD(ID,2) WHEN 0 THEN 1 ELSE 0 END, CASE WHEN MOD(ID,10)=0 THEN CAST(NULL AS DATE) WHEN ID=2 THEN DATE('2024-02-29') WHEN ID=3 THEN DATE('2024-12-31') ELSE DATE('2024-01-01') + ID DAYS END, CASE WHEN MOD(ID,10)=0 THEN CAST(NULL AS TIME) WHEN ID=3 THEN TIME('00:00:00') WHEN ID=4 THEN TIME('23:59:59') ELSE TIME('09:30:00') END, CASE WHEN ID=2 THEN TIMESTAMP('2024-02-29-23.59.59.123456') WHEN ID=3 THEN TIMESTAMP('2024-12-31-23.59.59.999999') ELSE TIMESTAMP('2024-01-01-08.30.15.123456') + ID DAYS END, CASE WHEN MOD(ID,5)=0 THEN CAST(NULL AS TIMESTAMP) ELSE TIMESTAMP('2024-01-01-11.05.10.654321') + ID DAYS END, CASE WHEN MOD(ID,10)=0 THEN 'NULL date time test' WHEN ID=2 THEN 'Leap year test' WHEN ID=3 THEN 'Year end midnight test' ELSE 'Normal employee' END FROM (VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10),(11),(12),(13),(14),(15),(16),(17),(18),(19),(20),(21),(22),(23),(24),(25),(26),(27),(28),(29),(30),(31),(32),(33),(34),(35),(36),(37),(38),(39),(40),(41),(42),(43),(44),(45),(46),(47),(48),(49),(50)) AS T(ID)"
```

Verify the count and representative cases:

```cmd
db2 "SELECT COUNT(*) AS TOTAL_ROWS FROM TEST_SCHEMA.EMPLOYEE_DATA"
db2 "SELECT EMP_ID, JOIN_DATE, LOGIN_TIME, CREATED_AT, UPDATED_AT FROM TEST_SCHEMA.EMPLOYEE_DATA WHERE EMP_ID IN (2,3,10,50) ORDER BY EMP_ID"
```

**Expected:** 50 rows. ID 2 includes leap day and `.123456`; ID 3 includes midnight and `.999999`; IDs 10 and 50 contain `NULL` date/time fields. Db2 may *display* `DATE` in a territory-dependent format (for example, `MM/DD/YYYY`).

## 4. Create the fresh TARGET database

```cmd
db2 terminate
db2 create database DBTG2
db2 connect to DBTG2
db2 "SELECT CURRENT SERVER AS DB_NAME, SESSION_USER AS LOGIN_USER FROM SYSIBM.SYSDUMMY1"
```

Confirm `DB_NAME = DBTG2`. **Do not manually create `TEST_SCHEMA` or `EMPLOYEE_DATA` in the target.** The migration framework should create them.

Check that the target test table is absent:

```cmd
db2 "SELECT TABSCHEMA, TABNAME, TYPE FROM SYSCAT.TABLES WHERE TABSCHEMA='TEST_SCHEMA' AND TABNAME='EMPLOYEE_DATA'"
```

**Expected:** `0 record(s) selected`. If the table already exists, **stop**; this framework's tested FULL path rejects a repeat FULL against an existing target table.

## 5. Grant and verify TARGET permissions

**Still connected to `DBTG2` as the authorized administrative account.** The following broad database authorities were used in the successful local E2E test:

```cmd
db2 "GRANT DBADM ON DATABASE TO USER DB2ADMIN"
db2 "GRANT DATAACCESS ON DATABASE TO USER DB2ADMIN"
db2 "GRANT ACCESSCTRL ON DATABASE TO USER DB2ADMIN"
```

Verify the granted authorities:

```cmd
db2 "SELECT GRANTEE, DBADMAUTH, DATAACCESSAUTH, ACCESSCTRLAUTH FROM SYSCAT.DBAUTH WHERE GRANTEE='DB2ADMIN'"
```

**Expected:** `DB2ADMIN` with `Y`, `Y`, `Y`. The administrative account needs suitable grant authority (in the earlier test, `ADMIN` had `SECADM`). These grants are deliberately broad for local testing; do not copy them into production without review. Advanced role grants or security-object migrations can require a **separate SECADM security-executor connection**; that setup is **not exercised** by this single-table fixture.

## 6. Grant and verify SOURCE read permission

Switch back to the **SOURCE database** before issuing this grant:

```cmd
db2 connect to DBSRC2
db2 "SELECT CURRENT SERVER AS DB_NAME, SESSION_USER AS LOGIN_USER FROM SYSIBM.SYSDUMMY1"
db2 "GRANT SELECTIN ON SCHEMA TEST_SCHEMA TO USER DB2ADMIN"
db2 "SELECT GRANTEE, SELECTINAUTH FROM SYSCAT.SCHEMAAUTH WHERE SCHEMANAME='TEST_SCHEMA' AND GRANTEE='DB2ADMIN'"
```

**Expected:** `DB_NAME = DBSRC2`, and `SELECTINAUTH = Y`. This is **one schema-level read grant**, not a separate `SELECT` grant for each table. Additional access or catalog privileges may be needed in environments with different security policies. No SOURCE `DATAACCESS` grant is required for the successful single-schema test described here.

## 7. Create the local migration YAML

Create `config/db2_test2.yaml` **relative to the repository root**, with example host/port/driver/user settings adjusted to your own environment:

```yaml
alerting:
  notifier: none

logging:
  file: logs/db2_test2.log
  level: INFO

migration:
  batch_size: 1000
  include_schemas:
    - TEST_SCHEMA
  mode: full

secrets:
  provider: env

source:
  engine: db2
  connection:
    database: DBSRC2
    driver: IBM DB2 ODBC DRIVER - DB2COPY1
    host: 127.0.0.1
    port: 25000
    username: db2admin
    password_secret: db2_source_2

target:
  engine: db2
  connection:
    database: DBTG2
    driver: IBM DB2 ODBC DRIVER - DB2COPY1
    host: 127.0.0.1
    port: 25000
    username: db2admin
    password_secret: db2_target_2
    source_engine: db2

validation:
  mode: count
  sample_size: 100
  strict_grant_parity: false
```

**Important:** `validation.mode: count` checks counts; it does **not** itself establish exact equality of all column values. That is why Section 10 compares all 10 columns independently. Keep this test YAML **local only** unless your repository explicitly approves a sanitized example. Do not commit secrets.

## 8. Set password secrets in PowerShell

**Terminal: PowerShell, from the Python migration project root** (not necessarily the Db2 installation directory):

```powershell
Set-Location "F:\Quantumatrix\Projects\P2Migration\Migration_platform"
```

The path above is **only the path used in the recorded test**. Replace it with your own checkout path (the directory containing `migration_platform/__main__.py` and `config/db2_test2.yaml`).

```powershell
$env:SECRET_db2_source_2 = ([System.Net.NetworkCredential]::new('db2admin', (Read-Host -AsSecureString -Prompt 'SOURCE DBSRC2 password'))).Password
$env:SECRET_db2_target_2 = ([System.Net.NetworkCredential]::new('db2admin', (Read-Host -AsSecureString -Prompt 'TARGET DBTG2 password'))).Password
```

Use the same PowerShell session for the migration run. These commands avoid *displaying* the password, but process environment variables contain plain-text secret values; protect your session and do not echo, log, or commit them. **Do not** assign `Read-Host -AsSecureString` directly to `$env:SECRET_...`: the environment variable needs the actual password string, not the string representation of a `SecureString` object.

## 9. Run the FULL migration once

Confirm that the target is fresh and the YAML points to `DBSRC2` → `DBTG2`. Then, still in PowerShell under `<repo-root>`:

```powershell
python -m migration_platform --config config/db2_test2.yaml --mode full --no-live-ui
```

**Expected application report:**

```text
Migration SUCCESS
Tables migrated : 1
Total rows      : 50
Migrated        : 50
Failed          : 0
Errors          : 0
```

Also confirm that the command does **not** end with a Python traceback. The app may start a local **report server** and ask you to press **Ctrl+C** to stop it; the displayed report-server address is environment-specific and is not a database credential or migration endpoint. Preserve the audit and JSON/HTML report paths shown for the run.

**If the run fails:** do not blindly rerun `--mode full`. An earlier failed attempt created an empty target table, and a second FULL run was refused. Inspect the exact error, the target objects and row count, and the audit/report before planning any reset. Do not automatically drop an existing target table.

## 10. Compare all SOURCE and TARGET values

**Terminal: Db2 Command Window (CMD).** Export the same 10 columns in the same primary-key order from both databases. Use distinct filenames for each practice run.

**SOURCE:**

```cmd
db2 connect to DBSRC2
db2 "EXPORT TO %TEMP%\dbsrc2_full.del OF DEL SELECT EMP_ID, EMP_NAME, DEPARTMENT, SALARY, IS_ACTIVE, JOIN_DATE, LOGIN_TIME, CREATED_AT, UPDATED_AT, REMARKS FROM TEST_SCHEMA.EMPLOYEE_DATA ORDER BY EMP_ID"
```

**TARGET:**

```cmd
db2 connect to DBTG2
db2 "EXPORT TO %TEMP%\dbtg2_full.del OF DEL SELECT EMP_ID, EMP_NAME, DEPARTMENT, SALARY, IS_ACTIVE, JOIN_DATE, LOGIN_TIME, CREATED_AT, UPDATED_AT, REMARKS FROM TEST_SCHEMA.EMPLOYEE_DATA ORDER BY EMP_ID"
```

Each export should report **50 rows exported**. Then compare the two local files:

```cmd
fc /b "%TEMP%\dbsrc2_full.del" "%TEMP%\dbtg2_full.del"
```

**Expected:**

```text
FC: no differences encountered
```

This proves that **these exported representations** of all 50 rows and 10 columns match byte-for-byte, including `DATE`, `TIME`, `TIMESTAMP(6)`, fractional seconds, and `NULL`s. Compare exports in a consistent client/export environment; this alone does not prove parity of indexes, constraints, privileges, or other database objects.

## 11. Compare table definitions and primary keys

Run the following **read-only** queries **once on `DBSRC2` and once on `DBTG2`**, explicitly connecting to the intended database each time:

```cmd
db2 connect to DBSRC2
db2 "SELECT CURRENT SERVER AS DB_NAME FROM SYSIBM.SYSDUMMY1"
db2 "SELECT COLNAME, TYPENAME, LENGTH, SCALE, NULLS FROM SYSCAT.COLUMNS WHERE TABSCHEMA='TEST_SCHEMA' AND TABNAME='EMPLOYEE_DATA' ORDER BY COLNO"
db2 "SELECT C.CONSTNAME, K.COLNAME FROM SYSCAT.TABCONST C JOIN SYSCAT.KEYCOLUSE K ON C.TABSCHEMA=K.TABSCHEMA AND C.TABNAME=K.TABNAME AND C.CONSTNAME=K.CONSTNAME WHERE C.TABSCHEMA='TEST_SCHEMA' AND C.TABNAME='EMPLOYEE_DATA' AND C.TYPE='P' ORDER BY K.COLSEQ"

db2 connect to DBTG2
db2 "SELECT CURRENT SERVER AS DB_NAME FROM SYSIBM.SYSDUMMY1"
db2 "SELECT COLNAME, TYPENAME, LENGTH, SCALE, NULLS FROM SYSCAT.COLUMNS WHERE TABSCHEMA='TEST_SCHEMA' AND TABNAME='EMPLOYEE_DATA' ORDER BY COLNO"
db2 "SELECT C.CONSTNAME, K.COLNAME FROM SYSCAT.TABCONST C JOIN SYSCAT.KEYCOLUSE K ON C.TABSCHEMA=K.TABSCHEMA AND C.TABNAME=K.TABNAME AND C.CONSTNAME=K.CONSTNAME WHERE C.TABSCHEMA='TEST_SCHEMA' AND C.TABNAME='EMPLOYEE_DATA' AND C.TYPE='P' ORDER BY K.COLSEQ"
```

**Expected metadata:** 10 columns with matching type names, lengths, scales, and nullability; `EMP_ID` is the PRIMARY KEY on both databases. Db2-generated **constraint names may differ** across databases: compare the key column and structure, not the generated name string.

Optional final count in **each** database:

```cmd
db2 "SELECT COUNT(*) AS TOTAL_ROWS FROM TEST_SCHEMA.EMPLOYEE_DATA"
```

## 12. Acceptance checklist

| Check | Expected result |
|---|---|
| SOURCE database, schema, and table | Created successfully |
| SOURCE fixture | 50 rows; date/time edge cases present |
| TARGET | Fresh database; test table initially absent |
| TARGET authorization | `DBADM`, `DATAACCESS`, `ACCESSCTRL` show `Y` for migration user |
| SOURCE authorization | `SELECTIN` shows `Y` for migration user on test schema |
| YAML and environment secrets | Correct databases, driver, host, port, user, secret names |
| FULL migration | 1 table, 50/50 rows, 0 failed, 0 errors; no Python traceback |
| Data export and compare | 50 rows per export; `FC: no differences encountered` |
| Datatypes | Source/target column metadata matches (10 columns) |
| DATE, TIME, TIMESTAMP(6), `NULL` | Exported values match for all 50 rows |
| Primary key | `EMP_ID` on both sides (generated names may differ) |
| Logs and reports | Run ID and evidence paths retained |

**Pass criterion:** Mark the local basic-datatype FULL migration test complete only when the CLI reports success, independent SOURCE/TARGET data comparison passes, and column/primary-key metadata matches. This test does **not** certify advanced Db2 objects, every security grant, or production-grade repeat-FULL behavior.

## 13. Reuse for future practice runs

For another run, replace **consistently**:

1. Source and target database names (for example `DBSRC3` / `DBTG3`).
2. YAML filename and local logging path (for example `config/db2_test3.yaml`).
3. Secret reference names in YAML and matching PowerShell environment-variable names (for example `db2_source_3` / `db2_target_3`).
4. Export filenames (for example `dbsrc3_full.del` / `dbtg3_full.del`).
5. Host, port, ODBC driver, DB login and repository path **only when different on that machine**.

You may keep `TEST_SCHEMA`, `EMPLOYEE_DATA`, and the 50-row fixture unchanged to repeat the same baseline, or use a different representative fixture and update the expected row counts and comparison commands accordingly.

### Common mistakes and how to avoid them

| Symptom | Likely cause / next check |
|---|---|
| `SQL1024N` (no database connection) | Run `db2 connect to <database>` before SQL statements |
| `SQL0104N` after `db2 connect DBSRC2` | Correct command: `db2 connect to DBSRC2` |
| `SQL30082N` reason 24 | Wrong login/password or secret resolution; verify credentials without printing them |
| `SQL0552N` on `CREATE SCHEMA` | TARGET migration user lacks necessary authority; check TARGET grants |
| `SQL0551N` on SOURCE `SELECT` | Check SOURCE schema `SELECTIN` / table read authorization |
| `FileNotFoundError: config/...yaml` | Run Python from the repository root, not the Db2 BIN directory |
| `Target table ... already exists` | A previous attempt left objects in TARGET; inspect before any reset or retry |
| `UnboundLocalError: create_secret_provider` | Previously caused by a duplicate function-local import in `migration_platform/__main__.py`; fix was made separately, then unit tests passed |
