# Secret Management — Practical User Guide

This guide is for actually **setting up secrets and running migrations**.

The simple rule is:

- Password value → stored in a secret provider.
- YAML → contains only the secret **name/reference**.
- Source and target can use **different secret providers**.
- Database engine and secret provider are independent.

Example:

```yaml
source:
  secret_provider: local_encrypted_file

target:
  secret_provider: azure_keyvault
```

This means:

```text
Source password → Local Encrypted Store
Target password → Azure Key Vault
```

---

# 1. Available secret providers

| Provider | Where the password is stored | Typical use |
|---|---|---|
| `env` | Environment variable | Simple/local testing |
| `local_encrypted_file` | Encrypted local file | Local machine / on-prem |
| `azure_keyvault` | Azure Key Vault | Azure/cloud |
| `aws_secrets_manager` | AWS Secrets Manager | AWS |
| `gcp_secret_manager` | Google Cloud Secret Manager | GCP |
| `hashicorp_vault` | HashiCorp Vault | Vault environments |

This guide mainly covers **Local Store** and **Azure Key Vault**.

---

# 2. Never put passwords in YAML

Do **not** do this:

```yaml
password: MyActualPassword123
```

Use:

```yaml
password_secret: mysql-source-password
```

`mysql-source-password` is only a name. The actual password is stored in the selected provider.

```text
YAML
  |
  | password_secret
  v
Selected secret provider
  |
  v
Actual password
  |
  v
Database connection
```

Passwords must not be placed in YAML, command-line arguments, logs, reports, Git commits, or documentation.

---

# 3. Local Encrypted Store — first-time setup

On Windows, run from the repository root:

```powershell
python -m migration_platform.secrets_cli set mysql-source-password
```

Enter the database password when prompted. The input is hidden.

The default Windows store is:

```text
%LOCALAPPDATA%\MigrationPlatform\secrets.enc
```

The encryption key is stored through the Windows OS keyring / Credential Manager.

You normally configure the Local Store **once per machine/user**.

---

# 4. Check the Local Store

### See secret names

```powershell
python -m migration_platform.secrets_cli list
```

Example:

```text
mysql-source-password
```

Only names are displayed.

### Verify a secret can be decrypted

```powershell
python -m migration_platform.secrets_cli verify mysql-source-password
```

Expected:

```text
Secret reference 'mysql-source-password' is present and decryptable.
```

### Change an existing password

Run:

```powershell
python -m migration_platform.secrets_cli set mysql-source-password
```

Enter the new password twice.

If the reference name stays the same, YAML does not change.

### Delete a secret

```powershell
python -m migration_platform.secrets_cli delete mysql-source-password
```

---

# 5. Add PostgreSQL or MSSQL passwords

The same Local Store can contain multiple secrets.

PostgreSQL:

```powershell
python -m migration_platform.secrets_cli set postgres-source-password
```

MSSQL:

```powershell
python -m migration_platform.secrets_cli set mssql-source-password
```

Target example:

```powershell
python -m migration_platform.secrets_cli set mysql-target-password
```

Possible names:

```text
mysql-source-password
mysql-target-password
postgres-source-password
postgres-target-password
mssql-source-password
mssql-target-password
```

YAML selects the required one:

```yaml
password_secret: mysql-source-password
```

---

# 6. What does `auto_create: false` mean?

Use:

```yaml
local_encrypted_file:
  key_source: keyring
  auto_create: false
```

It means:

> Migration expects the Local Store to already exist. Migration should not silently create a new empty store.

The normal flow is:

```text
First-time setup
      ↓
secrets_cli set
      ↓
Encrypted Store created
      ↓
Migration starts
      ↓
Migration reads the existing Store
```

If the store is missing, configure it first with the CLI.

The CLI `set` command is still allowed to create the store.

---

# 7. Azure Key Vault setup

## Step 1 — Login

```powershell
az login
```

Check the active account:

```powershell
az account show
```

Confirm the correct Azure subscription and tenant.

## Step 2 — Check the target secret

Example:

```powershell
az keyvault secret show `
  --vault-name <your-key-vault-name> `
  --name mysql-target-password `
  --query "id" `
  -o tsv
```

A successful result is a secret resource ID. The secret value is not printed.

---

# 8. Test Azure Key Vault through the application

Run:

```powershell
python -c "from core.secrets.azure_keyvault import AzureKeyVaultProvider; AzureKeyVaultProvider('https://<your-key-vault-name>.vault.azure.net/').get_secret('mysql-target-password'); print('Azure Key Vault secret is accessible.')"
```

Expected:

```text
Azure Key Vault secret is accessible.
```

This checks that the application's Azure Key Vault provider can retrieve the secret without displaying its value.

---

# 9. Check database connectivity

Local MySQL:

```powershell
Test-NetConnection 127.0.0.1 -Port 3306
```

Azure MySQL:

```powershell
Test-NetConnection <your-mysql-server>.mysql.database.azure.com -Port 3306
```

Look for:

```text
TcpTestSucceeded : True
```

This checks network reachability only. It does not prove that database authentication will succeed.

---

# 10. Main example — Local MySQL → Azure MySQL

This is the important mixed-provider scenario.

```text
Local MySQL
     |
     | source password
     v
Local Encrypted Store

          MIGRATION

Azure MySQL
     ^
     | target password
     |
Azure Key Vault
```

## Step 1 — Store the local password

```powershell
python -m migration_platform.secrets_cli set mysql-source-password
```

Verify:

```powershell
python -m migration_platform.secrets_cli verify mysql-source-password
```

## Step 2 — Check Azure target secret

```powershell
az login
```

Then:

```powershell
az keyvault secret show `
  --vault-name <your-key-vault-name> `
  --name mysql-target-password `
  --query "id" `
  -o tsv
```

## Step 3 — Check application access

```powershell
python -c "from core.secrets.azure_keyvault import AzureKeyVaultProvider; AzureKeyVaultProvider('https://<your-key-vault-name>.vault.azure.net/').get_secret('mysql-target-password'); print('Azure Key Vault secret is accessible.')"
```

## Step 4 — Check both database ports

```powershell
Test-NetConnection 127.0.0.1 -Port 3306
```

and:

```powershell
Test-NetConnection <your-mysql-server>.mysql.database.azure.com -Port 3306
```

Both should show:

```text
TcpTestSucceeded : True
```

---

# 11. YAML — Local MySQL → Azure MySQL

```yaml
source:
  engine: mysql
  secret_provider: local_encrypted_file

  connection:
    host: 127.0.0.1
    port: 3306
    database: mysql_migration_source
    username: mysql_test
    password_secret: mysql-source-password
    ssl: true

target:
  engine: mysql
  secret_provider: azure_keyvault

  connection:
    host: <your-mysql-server>.mysql.database.azure.com
    port: 3306
    database: mysql_migration_target
    username: mysql_admin
    password_secret: mysql-target-password
    ssl: true

migration:
  mode: full
  batch_size: 1000
  reconcile_target_schema: true

retry:
  max_retries: 3
  base_delay_seconds: 1.0
  max_delay_seconds: 30.0

secrets:
  provider: azure_keyvault

  local_encrypted_file:
    key_source: keyring
    keyring_service: migration-platform/secrets-key
    auto_create: false

  azure_keyvault:
    url: https://<your-key-vault-name>.vault.azure.net/

alerting:
  notifier: none

validation:
  mode: count
  sample_size: 1000

logging:
  level: INFO
```

The four important settings are:

```yaml
source:
  secret_provider: local_encrypted_file
```

```yaml
source:
  connection:
    password_secret: mysql-source-password
```

```yaml
target:
  secret_provider: azure_keyvault
```

```yaml
target:
  connection:
    password_secret: mysql-target-password
```

So the platform resolves:

```text
SOURCE
MySQL
  ↓
Local Store
  ↓
mysql-source-password
  ↓
Local MySQL password


TARGET
MySQL
  ↓
Azure Key Vault
  ↓
mysql-target-password
  ↓
Azure MySQL password
```

---

# 12. Run the migration

After all checks pass:

```powershell
python -m migration_platform --config config/mysql_local_to_azure.yaml --mode full --no-live-ui
```

Replace the config filename if your YAML has a different name.

Notice that the command contains **neither password**.

---

# 13. Local MySQL → Local MySQL

Both endpoints can use Local Store:

```yaml
source:
  engine: mysql
  secret_provider: local_encrypted_file
  connection:
    host: 127.0.0.1
    port: 3306
    database: source_db
    username: source_user
    password_secret: mysql-source-password

target:
  engine: mysql
  secret_provider: local_encrypted_file
  connection:
    host: 127.0.0.1
    port: 3307
    database: target_db
    username: target_user
    password_secret: mysql-target-password

secrets:
  provider: env
  local_encrypted_file:
    key_source: keyring
    auto_create: false
```

---

# 14. Local PostgreSQL → Local PostgreSQL

```yaml
source:
  engine: postgresql
  secret_provider: local_encrypted_file
  connection:
    host: 127.0.0.1
    port: 5432
    database: source_db
    username: source_user
    password_secret: postgres-source-password

target:
  engine: postgresql
  secret_provider: local_encrypted_file
  connection:
    host: 127.0.0.1
    port: 5433
    database: target_db
    username: target_user
    password_secret: postgres-target-password

secrets:
  provider: env
  local_encrypted_file:
    key_source: keyring
    auto_create: false
```

---

# 15. Local MSSQL → Local MSSQL

```yaml
source:
  engine: mssql
  secret_provider: local_encrypted_file
  connection:
    host: localhost
    port: 1433
    database: source_db
    username: source_user
    password_secret: mssql-source-password

target:
  engine: mssql
  secret_provider: local_encrypted_file
  connection:
    host: localhost
    port: 1434
    database: target_db
    username: target_user
    password_secret: mssql-target-password

secrets:
  provider: env
  local_encrypted_file:
    key_source: keyring
    auto_create: false
```

---

# 16. Azure MySQL → Local MySQL

Here the providers are reversed:

```text
Azure MySQL
    |
    | source password
    v
Azure Key Vault

          MIGRATION

Local MySQL
    ^
    | target password
    |
Local Encrypted Store
```

YAML:

```yaml
source:
  engine: mysql
  secret_provider: azure_keyvault
  connection:
    host: source.mysql.database.azure.com
    port: 3306
    database: source_db
    username: source_user
    password_secret: mysql-source-password

target:
  engine: mysql
  secret_provider: local_encrypted_file
  connection:
    host: 127.0.0.1
    port: 3306
    database: target_db
    username: target_user
    password_secret: mysql-target-password

secrets:
  provider: local_encrypted_file

  local_encrypted_file:
    key_source: keyring
    auto_create: false

  azure_keyvault:
    url: https://<vault-name>.vault.azure.net/
```

---

# 17. Azure MySQL → Azure MySQL

Both endpoints can use Azure Key Vault:

```yaml
source:
  engine: mysql
  secret_provider: azure_keyvault
  connection:
    host: source.mysql.database.azure.com
    port: 3306
    database: source_db
    username: source_user
    password_secret: mysql-source-password

target:
  engine: mysql
  secret_provider: azure_keyvault
  connection:
    host: target.mysql.database.azure.com
    port: 3306
    database: target_db
    username: target_user
    password_secret: mysql-target-password

secrets:
  provider: azure_keyvault
  azure_keyvault:
    url: https://<vault-name>.vault.azure.net/
```

---

# 18. ENV → ENV

Existing ENV configurations continue to work:

```yaml
source:
  engine: mysql
  secret_provider: env
  connection:
    host: 127.0.0.1
    port: 3306
    database: source_db
    username: source_user
    password_secret: mysql-source-password

target:
  engine: mysql
  secret_provider: env
  connection:
    host: 127.0.0.1
    port: 3307
    database: target_db
    username: target_user
    password_secret: mysql-target-password

secrets:
  provider: env
```

The environment variables are:

```text
SECRET_mysql-source-password
SECRET_mysql-target-password
```

Use an approved secret-injection mechanism to provide their values.

---

# 19. Global provider vs endpoint provider

You can define a global default:

```yaml
secrets:
  provider: azure_keyvault
```

If no endpoint override exists, that provider is used.

But you can override it:

```yaml
source:
  secret_provider: local_encrypted_file

target:
  secret_provider: azure_keyvault
```

The endpoint setting wins.

Simple rule:

```text
secrets.provider
       ↓
global default

source.secret_provider
       ↓
source override

target.secret_provider
       ↓
target override
```

---

# 20. Provider combination matrix

| Source | Target | Supported |
|---|---|---|
| ENV | ENV | Yes |
| ENV | Local Store | Yes |
| ENV | Azure Key Vault | Yes |
| Local Store | ENV | Yes |
| Local Store | Local Store | Yes |
| Local Store | Azure Key Vault | Yes |
| Azure Key Vault | ENV | Yes |
| Azure Key Vault | Local Store | Yes |
| Azure Key Vault | Azure Key Vault | Yes |

The selected database engines still need corresponding connectors.

---

# 21. Changing a password

If the database password changes but the secret name stays the same, YAML does not need to change.

For Local Store:

```powershell
python -m migration_platform.secrets_cli set mysql-source-password
```

For Azure Key Vault, update the corresponding Key Vault secret through the approved Azure process.

The important distinction is:

```text
password changes
    ↓
update secret value

secret name changes
    ↓
update provider + YAML reference
```

---

# 22. New machine setup

The Local Store is local to the Windows user/machine.

It is not automatically synchronized to another machine.

On a new machine:

```powershell
python -m migration_platform.secrets_cli set mysql-source-password
```

Then:

```powershell
python -m migration_platform.secrets_cli list
```

and:

```powershell
python -m migration_platform.secrets_cli verify mysql-source-password
```

Azure Key Vault remains centralized. The new machine only needs the correct Azure identity and permissions.

---

# 23. Troubleshooting

| Problem | What to check |
|---|---|
| Local Store missing | Run `python -m migration_platform.secrets_cli list`; initialize with `set` if needed |
| Secret not found | Compare YAML `password_secret` with `list` output |
| Secret cannot decrypt | Check keyring/key-source configuration |
| Azure login fails | Run `az account show`, then `az login` |
| Key Vault permission fails | Check Azure identity and secret-read permission |
| Azure secret not found | Check vault name and secret name |
| Source port unreachable | Run `Test-NetConnection <host> -Port <port>` |
| Target port unreachable | Check DNS, firewall, routing, Azure allowlist |
| Database authentication fails | Check username, secret name, actual provider value, and DB grants |
| Wrong provider used | Check `source.secret_provider`, `target.secret_provider`, and global `secrets.provider` |
| Wrong Key Vault URL | Check `secrets.azure_keyvault.url` |

Never print the password while troubleshooting.

---

# 24. Security rules

Always follow these rules:

```text
Password value       → secret provider
YAML                  → secret reference only
Logs                  → no password
Reports               → no password
Git                   → no password
CLI arguments         → no password
```

Never commit:

```text
secrets.enc
encryption keys
keyring contents
plaintext passwords
```

---

# 25. AWS / GCP / HashiCorp Vault

The repository also contains implementations for:

- AWS Secrets Manager
- Google Cloud Secret Manager
- HashiCorp Vault

They follow the same basic pattern:

```text
password_secret
      ↓
selected provider
      ↓
actual password
      ↓
database connector
```

Their actual use requires the corresponding credentials, permissions, SDK dependencies, and network access.

---

# 26. Final checklist — Local MySQL → Azure MySQL

Before running the migration:

### Local secret

```powershell
python -m migration_platform.secrets_cli list
```

Confirm:

```text
mysql-source-password
```

### Local secret verification

```powershell
python -m migration_platform.secrets_cli verify mysql-source-password
```

Expected:

```text
Secret reference 'mysql-source-password' is present and decryptable.
```

### Azure login

```powershell
az account show
```

### Azure target secret

```powershell
az keyvault secret show `
  --vault-name <your-key-vault-name> `
  --name mysql-target-password `
  --query "id" `
  -o tsv
```

### Application access to Azure

```powershell
python -c "from core.secrets.azure_keyvault import AzureKeyVaultProvider; AzureKeyVaultProvider('https://<your-key-vault-name>.vault.azure.net/').get_secret('mysql-target-password'); print('Azure Key Vault secret is accessible.')"
```

### Local MySQL connectivity

```powershell
Test-NetConnection 127.0.0.1 -Port 3306
```

### Azure MySQL connectivity

```powershell
Test-NetConnection <your-mysql-server>.mysql.database.azure.com -Port 3306
```

### YAML

Confirm:

```yaml
source:
  secret_provider: local_encrypted_file
  connection:
    password_secret: mysql-source-password

target:
  secret_provider: azure_keyvault
  connection:
    password_secret: mysql-target-password
```

### Run

```powershell
python -m migration_platform --config config/mysql_local_to_azure.yaml --mode full --no-live-ui
```

---

# 27. One-line summary

**Database engine tells the platform which database connector to use; `secret_provider` tells it where that endpoint's password comes from; `password_secret` tells it which password to retrieve.**

Example:

```text
Local MySQL
   +
Local Store
   +
mysql-source-password
        ↓
     Migration
        ↓
Azure MySQL
   +
Azure Key Vault
   +
mysql-target-password
```
