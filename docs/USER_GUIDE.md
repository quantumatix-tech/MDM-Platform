# Migration Platform — Complete User Guide

> **Project:** `migration-platform`
> **Path:** [`r:\unifide_migration_platform\migration-platform`](file:///r:/unifide_migration_platform/migration-platform)
> **Last Successful Run:** PostgreSQL to Azure PostgreSQL, 13.8s, 100% success

---

## Table of Contents

1. [What This Platform Does](#1-what-this-platform-does)
2. [Architecture Overview](#2-architecture-overview)
3. [Installation & Setup](#3-installation--setup)
4. [Supported Databases](#4-supported-databases)
5. [Configuration File Reference](#5-configuration-file-reference)
6. [Secrets & Passwords — All 6 Providers](#6-secrets--passwords--all-6-providers)
7. [Running a Migration](#7-running-a-migration)
8. [Migration Modes](#8-migration-modes)
9. [Migration Phases — What Happens Step-by-Step](#9-migration-phases--what-happens-step-by-step)
10. [Field Mappings & Column Exclusions](#10-field-mappings--column-exclusions)
11. [Validation](#11-validation)
12. [Alerting & Notifications](#12-alerting--notifications)
13. [Retry & Circuit Breaker](#13-retry--circuit-breaker)
14. [Logs & Audit Trail](#14-logs--audit-trail)
15. [Reports & Dashboard](#15-reports--dashboard)
16. [CDC Prerequisites](#16-cdc-prerequisites)
17. [Command-Line Reference](#17-command-line-reference)
18. [Example Configs for Every Scenario](#18-example-configs-for-every-scenario)
19. [Common Errors & Fixes](#19-common-errors--fixes)

---

## 1. What This Platform Does

The Migration Platform is an **enterprise-grade, schema-aware database migration tool**. It can:

- **Copy your entire database** — schema + data + constraints + views + functions + triggers + grants — in one command.
- **Stream real-time changes** from source to target using CDC (Change Data Capture), keeping two databases in sync with near-zero downtime.
- **Validate data integrity** after migration: row counts, checksums, or full row comparison.
- **Report** every run as a shareable HTML report with a live browser dashboard.
- **Alert** your team on Slack, Teams, email, or a custom webhook when a migration completes.

---

## 2. Architecture Overview

```
migration_platform/__main__.py  (CLI entry point)
         |
         v
core/orchestrator.py  (all 19 migration phases)
    |          |          |
    v          v          v
Source      Secret     Validator
Connector   Resolver   (count/checksum/full)
    |          |
    v          v
Target      Notifier
Connector   (Slack/Teams/Email/Webhook)
```

**Key files:**

| File | Purpose |
|---|---|
| [`migration_platform/__main__.py`](file:///r:/unifide_migration_platform/migration-platform/migration_platform/__main__.py) | CLI entry point |
| [`core/orchestrator.py`](file:///r:/unifide_migration_platform/migration-platform/core/orchestrator.py) | All migration phases (1,034 lines) |
| [`core/connectors/postgresql.py`](file:///r:/unifide_migration_platform/migration-platform/core/connectors/postgresql.py) | PostgreSQL source + target connector |
| [`core/secrets/`](file:///r:/unifide_migration_platform/migration-platform/core/secrets/) | All 6 secret providers |
| [`core/validator.py`](file:///r:/unifide_migration_platform/migration-platform/core/validator.py) | Data integrity validation |
| [`core/alerting.py`](file:///r:/unifide_migration_platform/migration-platform/core/alerting.py) | Slack/Teams/Email/Webhook alerting |
| [`core/retry.py`](file:///r:/unifide_migration_platform/migration-platform/core/retry.py) | Retry decorator + Circuit Breaker |
| [`config/my_config.yaml`](file:///r:/unifide_migration_platform/migration-platform/config/my_config.yaml) | Your active config |
| [`bootstrap.py`](file:///r:/unifide_migration_platform/migration-platform/bootstrap.py) | One-time dependency installer |

---

## 3. Installation & Setup

### Step 1 — Install All Dependencies

```powershell
# Run from: r:\unifide_migration_platform\migration-platform
python bootstrap.py
```

This installs everything in [`requirements.txt`](file:///r:/unifide_migration_platform/migration-platform/requirements.txt):

| Package | Used For |
|---|---|
| `psycopg[binary]` | PostgreSQL |
| `mysql-connector-python` + `mysql-replication` | MySQL + CDC |
| `pymongo` | MongoDB |
| `pyodbc` | SQL Server (MSSQL) |
| `azure-identity` + `azure-keyvault-secrets` | Azure Key Vault |
| `azure-cosmos` | Azure Cosmos DB |
| `pyyaml` | Config file parsing |
| `boto3` | AWS Secrets Manager |
| `google-cloud-secret-manager` | GCP Secret Manager |
| `hvac` | HashiCorp Vault |
| `cryptography` + `keyring` | Local encrypted secrets file |
| `requests` | Webhook / Slack / Teams alerts |

### Step 2 — Verify Drivers for Your Specific Config

```powershell
python bootstrap.py --config config/my_config.yaml
```

### Step 3 — MSSQL Only: Install ODBC Driver

```powershell
# Windows (Chocolatey)
choco install msodbcsql18

# Or download from Microsoft directly:
# https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
```

---

## 4. Supported Databases

### Source Databases (reads FROM)

| Engine Key | Database |
|---|---|
| `postgresql` | PostgreSQL (any version, local or cloud) |
| `mysql` | MySQL 5.7+ |
| `mongodb` | MongoDB 4.0+ |
| `mssql` | SQL Server (requires ODBC Driver 18) |

### Target Databases (writes TO)

| Engine Key | Database |
|---|---|
| `postgresql` | PostgreSQL (local, Azure DB, AWS RDS, etc.) |
| `mysql` | MySQL |
| `mongodb` | MongoDB |
| `mssql` | SQL Server |
| `cosmos_mongo` | Azure Cosmos DB (MongoDB API) |

### Cross-Database Migration Matrix

| Source to Target | Full | CDC |
|---|---|---|
| PostgreSQL to PostgreSQL | Yes | Yes |
| PostgreSQL to Azure PostgreSQL | Yes | Yes |
| MySQL to MySQL | Yes | Yes |
| MySQL to PostgreSQL | Yes | No |
| MongoDB to MongoDB | Yes | Yes |
| MongoDB to Cosmos DB | Yes | Yes |
| MSSQL to MSSQL | Yes | Yes |
| MSSQL to PostgreSQL | Yes | No |

---

## 5. Configuration File Reference

Every migration is driven by a **YAML config file**. Full schema: [`config/migration_config.schema.yaml`](file:///r:/unifide_migration_platform/migration-platform/config/migration_config.schema.yaml).

### Complete Annotated Config

```yaml
# SOURCE DATABASE
source:
  engine: postgresql        # postgresql | mysql | mongodb | mssql
  connection:
    host: localhost
    port: 5432
    database: my_source_db
    username: postgres
    password_secret: source_db_pass   # Secret name — NOT the password itself
    ssl: false                         # true = require SSL, false = disable
    ssl_ca_cert: /path/to/ca.crt      # Optional: CA cert path for SSL verification

# TARGET DATABASE
target:
  engine: postgresql        # postgresql | mysql | mongodb | mssql | cosmos_mongo
  connection:
    host: my-server.postgres.database.azure.com
    port: 5432
    database: my_target_db
    username: adminuser
    password_secret: target_db_pass
    ssl: true
    keepalives_idle: 30     # TCP keepalive seconds — required for Azure/cloud connections

# MIGRATION SETTINGS
migration:
  mode: full                # full | cdc-incremental | cdc-continuous
  batch_size: 1000          # Rows per INSERT batch. Lower = safer on slow networks
  field_mappings:           # Optional: rename or exclude columns
    - source_field: old_column_name
      target_field: new_column_name
    - source_field: sensitive_column
      exclude: true

# CDC SETTINGS (only for cdc-* modes)
cdc:
  poll_interval: 10         # Seconds between polling for new changes

# RETRY & RESILIENCE
retry:
  max_retries: 3
  base_delay_seconds: 1.0
  max_delay_seconds: 30.0
  circuit_breaker_max_failures: 5
  circuit_breaker_reset_timeout_seconds: 60.0

# SECRETS PROVIDER
secrets:
  provider: env             # env | azure_keyvault | aws_secrets_manager |
                            # gcp_secret_manager | hashicorp_vault | local_encrypted_file

# ALERTING
alerting:
  notifier: none            # none | slack | teams | webhook | email

# VALIDATION
validation:
  mode: count               # count | checksum | full
  sample_size: 1000         # Only used by checksum mode

# LOGGING
logging:
  level: INFO               # DEBUG | INFO | WARNING | ERROR
  file: migration.log       # Optional: additional log file
```

---

## 6. Secrets & Passwords — All 6 Providers

> [!IMPORTANT]
> Never put passwords directly in the config file. Always use `password_secret` with a name, then configure a provider.

### How It Works

In your config:
```yaml
password_secret: source_db_pass
```
The platform resolves the password from the configured secret provider using that name.

---

### Provider 1: Environment Variables (Default)

```yaml
secrets:
  provider: env
```

The platform looks for `SECRET_<name>` in the environment.

**PowerShell:**
```powershell
$env:SECRET_source_db_pass = "your_source_password"
$env:SECRET_target_db_pass = "your_target_password"
```

> [!CAUTION]
> Env vars are session-scoped — lost when you close PowerShell. Set them fresh before every run.

---

### Provider 2: Azure Key Vault

```yaml
secrets:
  provider: azure_keyvault
  azure_keyvault:
    url: https://my-keyvault.vault.azure.net/
```

Uses `DefaultAzureCredential` (Azure CLI login, managed identity, service principal, etc.)

**Setup:**
```powershell
az login
az keyvault secret set --vault-name my-keyvault --name source-db-pass --value "your_password"
```

The secret name in Key Vault must match the `password_secret` value in your config.

---

### Provider 3: AWS Secrets Manager

```yaml
secrets:
  provider: aws_secrets_manager
  aws_secrets_manager:
    region: us-east-1
```

**Setup:**
```bash
aws secretsmanager create-secret --name source_db_pass --secret-string "your_password"
```

---

### Provider 4: GCP Secret Manager

```yaml
secrets:
  provider: gcp_secret_manager
  gcp_secret_manager:
    project_id: my-gcp-project
```

---

### Provider 5: HashiCorp Vault

```yaml
secrets:
  provider: hashicorp_vault
  hashicorp_vault:
    url: https://vault.mycompany.com
    token: s.my-vault-token
    mount_point: secret    # Default is "secret"
```

---

### Provider 6: Local Encrypted File

Stores secrets in a Fernet-encrypted file. Good for air-gapped environments.

```yaml
secrets:
  provider: local_encrypted_file
  local_encrypted_file:
    file: secrets.enc
    key_source: env                         # env or keyring
    key_env_var: MIGRATION_SECRETS_KEY      # Holds the Fernet encryption key
    auto_create: true                       # Create file if missing
```

**Setup:**
```powershell
# 1. Generate a key (run once, save it!)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Set the key
$env:MIGRATION_SECRETS_KEY = "your-fernet-key-here"
```

---

## 7. Running a Migration

### Basic Syntax

```powershell
python -m migration_platform --config <path-to-config.yaml> --mode <mode>
```

### Your Setup (Quick Start)

```powershell
# Step 1: Set passwords (use SECRET_ prefix!)
$env:SECRET_source_db_pass = "root1234"
$env:SECRET_target_db_pass = "Mohit@991"

# Step 2: Run
python -m migration_platform --config config/my_config.yaml --mode full
```

### What You See While Running

```
╭── Migration Platform  Run: 2c156866...  Mode: FULL  Elapsed: 00:13 ──╮
╭── Overall Progress ───────────────────────────────────────────────────╮
│  |########################################|  100%  Current: completed  │
╭── Phases ────────────────╮  ╭── Table Statistics ──────────────────────╮
│ v Connect                │  │  Table          Source  Migrated  Rate   │
│ v Ensure Database        │  │  admissions        124       124  100%   │
│ v Extensions             │  │  appointments      104       104  100%   │
│ v Schemas                │  │  ...                                     │
```

---

## 8. Migration Modes

### `--mode full` — Full Database Copy

Copies everything: schema + all rows + constraints + views + functions + triggers + comments + grants. Runs once and exits.

**Use when:** First-time migration, complete database replacement, you can afford brief downtime.

```powershell
python -m migration_platform --config config/my_config.yaml --mode full
```

---

### `--mode cdc-incremental` — One CDC Pass

Performs a full initial sync, then reads the CDC stream **exactly once** to catch up any changes that happened during the sync, then exits.

**Use when:** Near-zero-downtime cutover. Run full sync, then run cdc-incremental right before cutover to apply the delta.

```powershell
python -m migration_platform --config config/my_config.yaml --mode cdc-incremental
```

---

### `--mode cdc-continuous` — Continuous Replication

Performs a full initial sync then **keeps streaming changes forever** until you press `Ctrl+C`.

**Use when:** Permanent sync (blue-green deployments, live replicas, zero-downtime migrations).

```powershell
python -m migration_platform --config config/my_config.yaml --mode cdc-continuous
# Press Ctrl+C to stop
```

> [!WARNING]
> CDC modes require `wal_level = logical` on the PostgreSQL source. See Section 16.

---

### `--no-live-ui` — Plain Output (CI/CD Friendly)

Disables the rich terminal panels. Outputs plain text — useful in pipelines.

```powershell
python -m migration_platform --config config/my_config.yaml --mode full --no-live-ui
```

---

### `--port` — Custom Dashboard Port

The live status dashboard defaults to port 8080.

```powershell
python -m migration_platform --config config/my_config.yaml --mode full --port 9090
```

---

## 9. Migration Phases — What Happens Step-by-Step

Every `--mode full` run executes these 19 phases in order:

| # | Phase | What Happens |
|---|---|---|
| 1 | **Connect** | Resolve secrets, connect source and target |
| 2 | **Ensure Database** | Create the target database if it does not exist |
| 3 | **Extensions** | Copy PostgreSQL extensions (uuid-ossp, pgcrypto, PostGIS, etc.) |
| 4 | **Schemas** | Create non-public schemas on the target |
| 5 | **Custom Types** | Copy ENUM, DOMAIN, COMPOSITE types |
| 6 | **Create Sequences** | Create all sequences before tables that reference them |
| 7 | **Create Tables** | Create all tables (with field mappings applied) |
| 8 | **Create Partitions** | Create child partition tables |
| 9 | **Migrate Data** | Copy all rows in batches of `batch_size` |
| 10 | **Apply Constraints** | Create indexes, foreign keys, check constraints |
| 11 | **Row-Level Security** | Copy RLS policies where enabled |
| 12 | **Advance Sequences** | Set sequences to `max(id)+1` so new INSERTs don't collide |
| 13 | **Views** | Create all SQL views |
| 14 | **Materialized Views** | Create and REFRESH all materialized views |
| 15 | **Functions** | Copy all functions and stored procedures |
| 16 | **Triggers** | Copy all triggers |
| 17 | **Comments** | Copy all column and table comments |
| 18 | **Grants** | Copy all GRANT statements |
| 19 | **Validate** | Run data integrity validation |

For **CDC modes**, phases 1-8 (initial sync) run first, then the CDC loop begins streaming changes.

---

## 10. Field Mappings & Column Exclusions

Control which columns are copied and how they are named on the target.

### Rename a Column

```yaml
migration:
  field_mappings:
    - source_field: customer_id
      target_field: client_id
```

### Exclude Sensitive Columns

```yaml
migration:
  field_mappings:
    - source_field: ssn
      exclude: true
    - source_field: credit_card_number
      exclude: true
```

### Rename and Exclude Together

```yaml
migration:
  field_mappings:
    - source_field: old_name
      target_field: new_name
    - source_field: internal_notes
      exclude: true
    - source_field: dob
      target_field: date_of_birth
```

> [!NOTE]
> Field mappings are applied before the target table is created, so the target schema already uses the new column names from the start.

---

## 11. Validation

After migration, the platform verifies source and target data match.

### Mode 1: `count` — Fast Row Count (Default)

Runs `SELECT COUNT(*)` on both source and target for every table.

```yaml
validation:
  mode: count
```

Best for: Large tables, quick sanity checks, production go/no-go.

---

### Mode 2: `checksum` — Data Fingerprint

Computes an MD5 hash of a sample of rows **inside the database** (no data pulled into Python).
Fast, memory-efficient, and catches data corruption or ordering differences.

```yaml
validation:
  mode: checksum
  sample_size: 1000
```

Best for: Verifying data correctness without a full scan.

---

### Mode 3: `full` — Row-by-Row Comparison

Pulls **all rows** from both source and target into memory, sorts them, and compares one-by-one.

```yaml
validation:
  mode: full
```

> [!WARNING]
> Very slow and memory-intensive for large tables. Use only for small databases or final confirmation.

---

## 12. Alerting & Notifications

### Disable Alerting (Default)

```yaml
alerting:
  notifier: none
```

### Slack

```yaml
alerting:
  notifier: slack
  webhook_url: https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXX
```

### Microsoft Teams

```yaml
alerting:
  notifier: teams
  webhook_url: https://outlook.office.com/webhook/YOUR/TEAMS/WEBHOOK
```

### Generic Webhook (POST JSON)

```yaml
alerting:
  notifier: webhook
  webhook_url: https://your-server.com/migration-callback
```

Payload sent: `{"phase": "run_full", "status": "completed", "details": {...}}`

### Email (SMTP)

```yaml
alerting:
  notifier: email
  smtp_server: smtp.gmail.com
  smtp_port: 587
  sender: migration-bot@yourcompany.com
  recipients:
    - team@yourcompany.com
    - dba@yourcompany.com
```

---

## 13. Retry & Circuit Breaker

Built-in resilience for flaky networks and transient failures.

### Retry Config

```yaml
retry:
  max_retries: 3               # Number of retry attempts
  base_delay_seconds: 1.0      # Wait before first retry
  max_delay_seconds: 30.0      # Maximum wait between retries
```

Retry uses **exponential backoff with jitter**:
- Attempt 1: ~1s wait
- Attempt 2: ~2s wait
- Attempt 3: ~4s wait

### Circuit Breaker

Prevents hammering a failing target. After too many consecutive failures, the circuit "opens" and immediately rejects requests for a cooldown period.

```yaml
retry:
  circuit_breaker_max_failures: 5        # Open after 5 consecutive failures
  circuit_breaker_reset_timeout_seconds: 60.0   # Try again after 60s
```

States: `closed` (normal) → `open` (rejecting) → `half_open` (testing recovery)

---

## 14. Logs & Audit Trail

Every run generates a structured **JSONL audit log** in the `logs/` directory.

### Location

```
logs/<run_id>.jsonl
```

### Read the Latest Log

```powershell
Get-Content (Get-ChildItem logs\*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
```

### Sample Log Lines

```jsonl
{"timestamp": "...", "run_id": "2c156866...", "phase": "connect",        "status": "success"}
{"timestamp": "...", "run_id": "2c156866...", "phase": "validate_count", "status": "pass", "details": {"object": "admissions", "source": 124, "target": 124}}
{"timestamp": "...", "run_id": "2c156866...", "phase": "run_full",       "status": "completed", "details": {"duration_s": 13.8}}
```

### Write an Additional Log File

```yaml
logging:
  level: INFO     # DEBUG | INFO | WARNING | ERROR
  file: migration_run.log
```

---

## 15. Reports & Dashboard

### HTML and JSON Reports

After every run, reports are saved automatically:

```
reports/<run_id>.html    — Visual report (opens in browser automatically)
reports/<run_id>.json    — Machine-readable version of all phases and stats
```

### Live Status Dashboard

While migration runs, a web dashboard shows live progress at:

```
http://localhost:8080/
```

After migration completes, the server **stays alive** and serves all past reports:

```
http://localhost:8080/reports/
```

Teammates on the same network can also access it:

```
http://<your-IP>:8080/reports/
```

(Your network IP is printed in the terminal after each run.)

### Custom Port

```powershell
python -m migration_platform --config config/my_config.yaml --mode full --port 9090
```

### Upload to Azure Blob Storage (Optional — Shareable Link)

```yaml
reporting:
  azure_blob:
    connection_string: "DefaultEndpointsProtocol=https;AccountName=...;AccountKey=..."
    container: migration-reports
```

After upload, a shareable public URL is printed in the terminal with an expiry date.

---

## 16. CDC Prerequisites

### PostgreSQL: Enable Logical Replication

CDC requires `wal_level = logical` on the source.

**Check:**
```sql
SHOW wal_level;
-- Must say: logical
```

**If it says `replica` or `minimal`, the platform auto-fixes it:**
1. Edits `postgresql.conf` to set `wal_level = logical`
2. Restarts the PostgreSQL service

> [!WARNING]
> Auto-fix requires running PowerShell **as Administrator**. If it fails, fix manually:

**Windows (manual):**
```powershell
# Edit: C:\Program Files\PostgreSQL\<version>\data\postgresql.conf
# Add line: wal_level = logical
# Then restart:
Restart-Service postgresql-x64-17   # Adjust version number
```

**Linux (manual):**
```bash
sudo nano /etc/postgresql/17/main/postgresql.conf
# Set: wal_level = logical
sudo systemctl restart postgresql
```

**Verify:**
```sql
SHOW wal_level;   -- Should now say: logical
```

### Azure PostgreSQL for CDC

Azure Database for PostgreSQL Flexible Server has `wal_level = logical` by default. No action needed.

---

## 17. Command-Line Reference

```
python -m migration_platform [OPTIONS]

Required:
  --config PATH          Path to YAML config file

Optional:
  --mode MODE            Migration mode (default: full)
                           full              Full schema + data copy
                           cdc-incremental   Full sync + one CDC pass
                           cdc-continuous    Full sync + stream forever (Ctrl+C to stop)

  --port INT             Status dashboard port (default: 8080)

  --no-live-ui           Disable rich terminal panels, output plain text
                         Use for CI/CD pipelines or log capture
```

### All Command Examples

```powershell
# Full migration
python -m migration_platform --config config/my_config.yaml --mode full

# Full migration, custom port, plain output
python -m migration_platform --config config/my_config.yaml --mode full --port 9000 --no-live-ui

# One CDC pass (initial sync + catch up delta)
python -m migration_platform --config config/my_config.yaml --mode cdc-incremental

# Continuous CDC (runs until Ctrl+C)
python -m migration_platform --config config/my_config.yaml --mode cdc-continuous

# Install all dependencies
python bootstrap.py

# Check only drivers needed for your config
python bootstrap.py --config config/my_config.yaml

# Verify drivers without reinstalling
python bootstrap.py --skip-pip
```

---

## 18. Example Configs for Every Scenario

### Scenario A: PostgreSQL to PostgreSQL (Local)

```yaml
source:
  engine: postgresql
  connection:
    host: localhost
    port: 5432
    database: source_db
    username: postgres
    password_secret: source_pass
    ssl: false

target:
  engine: postgresql
  connection:
    host: localhost
    port: 5433
    database: target_db
    username: postgres
    password_secret: target_pass
    ssl: false

migration:
  mode: full
  batch_size: 1000

secrets:
  provider: env

validation:
  mode: count

alerting:
  notifier: none

logging:
  level: INFO
```

**Run:**
```powershell
$env:SECRET_source_pass = "password1"
$env:SECRET_target_pass = "password2"
python -m migration_platform --config config/local_pg.yaml --mode full
```

---

### Scenario B: PostgreSQL to Azure PostgreSQL

```yaml
source:
  engine: postgresql
  connection:
    host: localhost
    port: 5432
    database: eds_db1
    username: postgres
    password_secret: source_db_pass
    ssl: false

target:
  engine: postgresql
  connection:
    host: dmt-postgre-test.postgres.database.azure.com
    port: 5432
    database: eds_db
    username: dmt_user
    password_secret: target_db_pass
    ssl: true
    keepalives_idle: 30

migration:
  mode: full
  batch_size: 500

retry:
  max_retries: 5
  base_delay_seconds: 2.0
  max_delay_seconds: 60.0

secrets:
  provider: env

alerting:
  notifier: slack
  webhook_url: https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK

validation:
  mode: checksum
  sample_size: 1000

logging:
  level: INFO
  file: azure_migration.log
```

---

### Scenario C: MySQL to MySQL

```yaml
source:
  engine: mysql
  connection:
    host: localhost
    port: 3306
    database: old_db
    username: root
    password_secret: mysql_source_pass
    ssl: false

target:
  engine: mysql
  connection:
    host: new-server.example.com
    port: 3306
    database: new_db
    username: admin
    password_secret: mysql_target_pass
    # Optional. Defaults to the target account returned by SELECT CURRENT_USER().
    # routine_definer: admin@%
    ssl: true

migration:
  mode: full
  batch_size: 1000

secrets:
  provider: env

validation:
  mode: count
```

For MySQL targets, migrated functions, procedures, triggers, and events with an
explicit `DEFINER` are recreated under the account returned by `SELECT
CURRENT_USER()` on the target. Set `target.connection.routine_definer` to an
explicit `user@host` only when the target account policy requires a different
existing account.

---

### Scenario D: MongoDB to Azure Cosmos DB

```yaml
source:
  engine: mongodb
  connection:
    host: localhost
    port: 27017
    database: my_mongo_db
    username: admin
    password_secret: mongo_pass
    ssl: false

target:
  engine: cosmos_mongo
  connection:
    host: my-cosmos.mongo.cosmos.azure.com
    port: 10255
    database: my_cosmos_db
    username: my-cosmos
    password_secret: cosmos_pass
    ssl: true

migration:
  mode: full
  batch_size: 100

secrets:
  provider: azure_keyvault
  azure_keyvault:
    url: https://my-vault.vault.azure.net/

validation:
  mode: count
```

---

### Scenario E: Continuous CDC Sync

```yaml
source:
  engine: postgresql
  connection:
    host: prod-db.internal
    port: 5432
    database: production_db
    username: replication_user
    password_secret: prod_db_pass
    ssl: true

target:
  engine: postgresql
  connection:
    host: standby-db.internal
    port: 5432
    database: production_db
    username: admin
    password_secret: standby_pass
    ssl: true
    keepalives_idle: 30

migration:
  mode: cdc-continuous
  batch_size: 500

cdc:
  poll_interval: 5

secrets:
  provider: env

alerting:
  notifier: teams
  webhook_url: https://outlook.office.com/webhook/YOUR/TEAMS/URL

validation:
  mode: count

logging:
  level: INFO
  file: cdc_sync.log
```

**Run:**
```powershell
$env:SECRET_prod_db_pass = "prod_password"
$env:SECRET_standby_pass = "standby_password"
python -m migration_platform --config config/cdc_sync.yaml --mode cdc-continuous
```

---

### Scenario F: Column Rename + Sensitive Data Exclusion

```yaml
source:
  engine: postgresql
  connection:
    host: localhost
    port: 5432
    database: patients_db
    username: postgres
    password_secret: db_pass
    ssl: false

target:
  engine: postgresql
  connection:
    host: cloud-db.example.com
    port: 5432
    database: patients_v2
    username: admin
    password_secret: cloud_pass
    ssl: true

migration:
  mode: full
  batch_size: 500
  field_mappings:
    - source_field: patient_id
      target_field: client_id
    - source_field: ssn
      exclude: true
    - source_field: credit_card
      exclude: true
    - source_field: dob
      target_field: date_of_birth

secrets:
  provider: env

validation:
  mode: checksum
  sample_size: 500
```

---

## 19. Common Errors & Fixes

### Error: Secret not found in environment

```
Secret 'source_db_pass' not found in environment (looked up as SECRET_source_db_pass)
```

**Cause:** Missing `SECRET_` prefix when setting the env var.

**Fix:**
```powershell
$env:SECRET_source_db_pass = "your_password"
$env:SECRET_target_db_pass = "your_password"
```

---

### Error: Connection refused

```
connection refused / could not connect to server
```

**Cause:** Database not running, or wrong host/port.

**Fix:** Verify the database is up:
```powershell
pg_isready -h localhost -p 5432
```
Double-check `host`, `port`, `database`, `username` in your config.

---

### Error: SSL connection required

```
SSL SYSCALL error / SSL connection required
```

**Cause:** Config has `ssl: false` but the server enforces SSL (always the case with Azure).

**Fix:**
```yaml
target:
  connection:
    ssl: true
```

---

### Error: wal_level is not logical (CDC only)

**Cause:** Logical replication not enabled on the source PostgreSQL.

**Fix:**
```powershell
# Run as Administrator
Restart-Service postgresql-x64-17
```

Then verify:
```sql
SHOW wal_level;   -- must be: logical
```

---

### Error: Circuit breaker open

```
Circuit breaker open for core.connectors.postgresql...
```

**Cause:** Too many consecutive failures opened the circuit breaker.

**Fix:** Wait 60 seconds (default reset timeout), or increase the threshold:
```yaml
retry:
  circuit_breaker_max_failures: 10
  circuit_breaker_reset_timeout_seconds: 30.0
```

---

### Error: Unknown source engine

```
Unknown source engine: 'X'. Available: ['postgresql', 'mysql', 'mongodb', 'mssql']
```

**Fix:** Use only valid engine keys: `postgresql`, `mysql`, `mongodb`, `mssql` for source; add `cosmos_mongo` for target.

---

### Error: ODBC Driver not found (MSSQL)

**Fix:**
```powershell
choco install msodbcsql18
# Or: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
```

---

### Validation: mismatch after migration

**Debug steps:**
1. Open the HTML report: `reports/<run_id>.html`
2. Check audit log: `logs/<run_id>.jsonl`
3. Look at the "Failed" column in the Table Statistics panel
4. Try a deeper validation mode:
```yaml
validation:
  mode: checksum
  sample_size: 2000
```

---

*This guide was generated by full source analysis of the [`migration-platform`](file:///r:/unifide_migration_platform/migration-platform) codebase. All options and behaviors reflect the actual implemented code.*
