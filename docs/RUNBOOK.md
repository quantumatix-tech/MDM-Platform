# Runbook — Unified Migration Platform

## Quick Start

1. Install dependencies: `python bootstrap.py` (or `make setup` if available)
2. Create a config file (see `config/examples/sample_config.yaml`)
3. Run:
   - `python -m migration_platform --config config.yaml --mode full`
   - `python -m migration_platform --config config.yaml --mode cdc-incremental`
   - `python -m migration_platform --config config.yaml --mode cdc-continuous`
4. Open `http://localhost:8080/status` for live progress
5. Find the HTML/JSON report in `reports/<run_id>.html` and `reports/<run_id>.json`

---

## Secret Providers

The platform is cloud-agnostic. Choose a secret provider via `secrets.provider` in config:

| Provider | Config value | Cloud required | Notes |
|----------|-------------|----------------|-------|
| Environment variables | `env` | No | Default. Secrets read from `SECRET_<NAME>` env vars. |
| AWS Secrets Manager | `aws_secrets_manager` | Yes (AWS) | Configure `secrets.aws_secrets_manager.region`. Requires `boto3`. |
| GCP Secret Manager | `gcp_secret_manager` | Yes (GCP) | Configure `secrets.gcp_secret_manager.project_id`. Requires `google-cloud-secret-manager`. |
| HashiCorp Vault | `hashicorp_vault` | No | Self-hosted or any cloud. Configure `url`, `token`, and optional `mount_point`. Requires `hvac`. Recommended for no-cloud environments. |
| Local Encrypted File | `local_encrypted_file` | No | Zero-cloud fallback. Secrets stored in an AES-encrypted file. Decryption key from env var or OS keyring. Requires `cryptography` and optionally `keyring`. |
| Azure Key Vault | `azure_keyvault` | Yes (Azure) | Configure `url` and optional `credential_secret_name`. |

### No-Cloud Secret Storage

For fully air-gapped or cloud-free environments, use **HashiCorp Vault** or **Local Encrypted File**:

- **HashiCorp Vault**: Run Vault on your own infrastructure (any OS, any cloud, or bare metal). The platform connects via HTTP and never touches a public cloud.
- **Local Encrypted File**: Secrets are encrypted at rest with a Fernet key. The key is supplied via an environment variable or OS keyring and is never stored in the encrypted file. This works with zero network calls.

### Local Encrypted File Setup

Generate a key and store it:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export MIGRATION_SECRETS_KEY="<generated-key>"
```

Create the encrypted secrets file:

```bash
python -c "
from core.secrets.local_encrypted_file import LocalEncryptedFileProvider
p = LocalEncryptedFileProvider(auto_create=True)
"
```

Add secrets by editing `secrets.enc` directly (it is an encrypted JSON blob) or by extending `LocalEncryptedFileProvider` with a CLI write helper.

---

## Bootstrap

Run `python bootstrap.py` to install Python dependencies and verify database drivers.

```bash
python bootstrap.py                              # check all engines
python bootstrap.py --config config.yaml         # check only engines used in config
python bootstrap.py --skip-pip                   # skip pip install (already done)
```

For MSSQL targets, the script will print the exact OS-specific command to install ODBC Driver 18 if it is missing:

- **Windows**: `choco install msodbcsql18`
- **macOS**: `brew tap microsoft/mssql-release && brew install msodbcsql18`
- **Linux**: `curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add - ...`

Docker does not require this step — the `docker/Dockerfile` installs the driver at build time.

---

## Migration Modes

| Mode | Config value | Behavior |
|------|-------------|----------|
| `full` | `migration.mode: full` | One-time bulk snapshot. |
| `cdc-incremental` | `migration.mode: cdc-incremental` | One CDC catch-up pass, then exit. Good for cron/scheduled sync. |
| `cdc-continuous` | `migration.mode: cdc-continuous` | Continuous CDC loop. Near-real-time replication. |

CLI overrides:
```bash
python -m migration_platform --config config.yaml --mode full
python -m migration_platform --config config.yaml --mode cdc-incremental
python -m migration_platform --config config.yaml --mode cdc-continuous
```

---

## Status Endpoint

The platform starts an HTTP status server on `http://localhost:8080/status` automatically during every run.

Response:
```json
{
  "run_id": "20240101-120000-abc123",
  "phase": "migrating: orders (3/12 objects)",
  "progress": 45,
  "errors": []
}
```

- `phase`: current activity string
- `progress`: 0-100 percentage complete
- `errors`: list of failures encountered so far

---

## Reports

Every run produces two files under `reports/`:

- `<run_id>.html` — self-contained report with inline CSS. Open in any browser. Contains run metadata, per-object migration results, validation status, and full error text.
- `<run_id>.json` — machine-readable twin for tooling or archival.

The CLI prints the HTML report path at the end of the run:

```
Report: reports/20240101-120000-abc123.html
```

---

## Common Failure Modes

### Authentication Failures
- **Symptom**: Connection refused or authentication error on connector `connect()`
- **Cause**: Incorrect credentials, expired secrets, or missing environment variables
- **Fix**: Verify `password_secret` resolves correctly via the configured secret provider. Check that `SECRET_<name>` environment variables are set for `env` provider. For Azure Key Vault, verify the credential has `GET` permission on the vault. For Vault, verify the token is valid and the path exists at the configured mount point.

### DNS Flakiness
- **Symptom**: Intermittent connection failures, especially during initial connect
- **Cause**: DNS resolution timeouts or transient DNS failures
- **Fix**: The `retry_with_backoff` decorator handles transient DNS failures automatically. If persistent, check DNS configuration and network connectivity to the database host.

### Throttling / Rate Limiting (429)
- **Symptom**: Cosmos DB or MongoDB operations return 429 errors
- **Cause**: Exceeding provisioned RU/s or request rate limits
- **Fix**: The connectors use exponential backoff with jitter by default. If throttling persists, increase provisioned throughput or reduce batch size in `migration.batch_size`.

### Secret Not Found
- **Symptom**: `KeyError` when resolving `password_secret`
- **Cause**: The secret name in config does not match the environment variable name or secret manager path
- **Fix**: Ensure the `password_secret` value matches the secret name. For env provider, the variable must be `SECRET_<name>`. For Vault, verify the path under the mount point. For AWS/GCP, verify the secret exists and the caller has `GetSecretValue`/`accessSecretVersion` permission.

### TLS Certificate Validation
- **Symptom**: SSL handshake failures on connect
- **Cause**: Self-signed certificates or expired CA certificates
- **Fix**: For local dev, set `ssl: false` in config. For production, ensure the CA certificate is available and `ssl_ca_cert` is configured. Never disable TLS in production.

---

## Connector-Specific Runbooks

### PostgreSQL
- **CDC Setup**: Requires `pgoutput` or `wal2json` plugin installed on the source server. Logical replication must be enabled (`wal_level = logical`).
- **Upsert**: Uses `INSERT ... ON CONFLICT DO UPDATE`. Ensure target table has a primary key or unique constraint for conflict resolution.

### MongoDB
- **CDC Setup**: Requires replica set mode. The oplog is only available on replica sets.
- **ObjectId Handling**: The connector uses native `ObjectId` type for comparisons, never string comparison.

### MySQL
- **CDC Setup**: Requires binary logging enabled (`binlog_format=ROW`). The CDC engine reads binlog position with timeouts to avoid idle hangs.
- **Upsert**: Uses `INSERT ... ON DUPLICATE KEY UPDATE`. Requires a unique key or primary key on the target table.

### MSSQL
- **CDC Setup**: Requires SQL Server CDC feature enabled on the database and target tables. The CDC engine resumes from the last checkpoint LSN to avoid unbounded query growth.
- **Upsert**: Uses `MERGE` statement. Ensure the target table has a primary key.
- **Docs**: See `docs/mssql/README.md` for MSSQL-specific E2E documentation.

### Cosmos DB for MongoDB
- **Rate Limiting**: Cosmos DB enforces RU limits. The connector uses exponential backoff on 429 responses.
- **Document Size**: Cosmos MongoDB API enforces a 2 MB document size limit. The connector checks document size before upsert and reports violations.

---

## Operational Procedures

### Starting a Migration
1. Review the pre-migration assessment report (`run_assessment()`)
2. Verify all secrets are accessible
3. Run `python bootstrap.py --config config.yaml` to verify drivers
4. Run the migration with the desired mode
5. Review validation results and the generated HTML report before marking as complete

### Rolling Back
**Not yet implemented.** `rollback()` raises `NotImplementedError`. To undo a migration, manually drop target objects or restore from a database snapshot. This will be implemented in a future release.

### Monitoring
- The status server exposes `/status` on port 8080 during every run
- Response includes `run_id`, `phase`, `progress`, and `errors`
- Alerting notifiers can be configured to fire on migration failure, validation failure, and CDC errors

---

## Chaos / Resilience Testing

These are manual procedures to validate that the platform is not just "correctly designed" but actually reliable under failure conditions.

### Mid-Migration Kill and Resume
1. Start a full migration against a source with known data.
2. While rows are being written, kill the process with `kill -9` (or Task Manager End Process on Windows).
3. Re-run the migration with the same config.
4. Verify:
   - No duplicate rows in the target (idempotency).
   - No missing rows (all source rows eventually appear in target).
   - Validation passes on the second run.

### Network Drop Simulation
1. Start a migration.
2. Disconnect the network or block the source/target port using firewall rules.
3. The connector's `retry_with_backoff` should handle transient failures and resume.
4. Restore the network.
5. Verify the migration completes successfully.

### CDC Sustained Load
1. Start a `cdc-continuous` migration.
2. Generate sustained write load on the source (inserts, updates, deletes).
3. Verify the CDC loop keeps up — `total_applied` should grow monotonically.
4. Stop the write load and verify the loop drains remaining events.

### Secret Provider Transient Failure
1. Configure a cloud secret provider (AWS, GCP, or Vault).
2. Simulate a transient network blip during secret resolution (e.g., block outbound traffic briefly).
3. Verify the provider retries and eventually resolves the secret, rather than failing immediately.
