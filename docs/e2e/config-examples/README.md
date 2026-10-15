# Migration Platform — E2E Configuration Examples

This directory contains illustrative configuration templates for running end-to-end
migration tests with MSSQL, PostgreSQL, and MySQL.

**These are documentation examples, not production configs.** Copy and adapt them
to your environment before use.

---

## Files

| File | Engine | Description |
|------|--------|-------------|
| `mssql-example.yaml` | MSSQL | MSSQL-to-MSSQL migration template |
| `postgresql-example.yaml` | PostgreSQL | PostgreSQL-to-PostgreSQL migration template |
| `mysql-example.yaml` | MySQL | MySQL-to-MySQL migration template |

---

## How to Use an Example

1. **Copy** the relevant example to a new file in `config/`:
   ```powershell
   Copy-Item docs/e2e/config-examples/mssql-example.yaml config/my_mssql_migration.yaml
   ```

2. **Edit** the new file and replace all `<PLACEHOLDER>` values with your actual
   connection details:
   - `<SOURCE_HOST>` / `<TARGET_HOST>` — database server IP or hostname
   - `<SOURCE_DATABASE>` / `<TARGET_DATABASE>` — database names
   - `<SOURCE_USERNAME>` / `<TARGET_USERNAME>` — database usernames
   - `<SCHEMA_1>`, `<SCHEMA_2>` — schema names to migrate (or remove `include_schemas` to discover all)

3. **Set environment variables** for secrets before running:
   ```powershell
   $env:SECRET_source_db_pass = "<your-source-password>"
   $env:SECRET_target_db_pass = "<your-target-password>"
   ```
   Never put real passwords in the YAML file. The `secrets.provider: env` setting
   tells the platform to read `SECRET_<password_secret>` from the environment.

4. **Run** the migration:
   ```powershell
   python -m migration_platform --config config/my_mssql_migration.yaml --mode full --no-live-ui
   ```

---

## Key Configuration Concepts

### Separate Source and Target Connections

The `source` and `target` sections are completely independent. They can point to:
- Different servers (local → cloud, cloud → local)
- Different engines (when cross-engine migration is supported)
- The same server with different databases

### `password_secret` and Environment Variables

The `password_secret` value is a **reference name**, not the password itself.
With `secrets.provider: env`, the platform looks for `SECRET_<password_secret>`
in the environment.

Example:
```yaml
password_secret: source_db_pass
```
Requires:
```powershell
$env:SECRET_source_db_pass = "<actual-password>"
```

### SSL Settings

**Default is `ssl: true` for all engines.** You MUST explicitly set `ssl: false`
for local databases without TLS, otherwise the connection will attempt TLS and fail.

| Setting | When to Use |
|---------|-------------|
| `ssl: false` | Local databases without TLS (REQUIRED for local) |
| `ssl: true` | Cloud databases (Azure, AWS RDS) or any server requiring TLS (default) |
| `ssl_ca_cert` | Optional path to CA certificate for verification (PostgreSQL, MySQL) |
| `tls` (MySQL) | Nested object for advanced TLS: `enabled`, `ca_path`, `cert_path`, `key_path`, `verify_cert`, `verify_identity` |

**MSSQL note**: `trust_server_certificate` is NOT a separate config option.
It is automatically derived: `true` when `ssl: false`, `false` when `ssl: true`.

### Schema Filters (`include_schemas`)

The `migration.include_schemas` setting controls which schemas are discovered.

- **Omitted** or `[]` (empty):
  - PostgreSQL: defaults to `["public"]` only
  - MSSQL: discovers all user schemas (no default filter)
  - MySQL: not used (MySQL uses databases, not schemas)
- **Listed** (e.g., `["sales", "billing"]`): migrate only the specified schemas

### `source_engine` (Target Connection)

**Do not set `source_engine` in the YAML.** It is automatically injected by the
platform at runtime (`main.py` line 128) based on the source engine type. The
examples previously showed it for documentation purposes, but it will be
overwritten.

It is required for correct **type mapping** when source and target engines are
the same. It tells the target connector which source engine's types to expect,
so the type mapping registry can apply the right conversions (e.g.,
`postgresql:postgresql:integer` → `integer`).

### MySQL: `preserve_source_definer` and `routine_definer`

- `preserve_source_definer: true` — keeps the source `DEFINER` account on migrated
  routines, triggers, and events (only works if that account exists on target)
- `routine_definer: "user@host"` — overrides the `DEFINER` for all migrated
  routines/triggers/events with a specific account

### MySQL: `security_users`

Optional allowlist for discovering MySQL user accounts and global permissions.
Only used when explicitly migrating users/roles. Place under `target.connection`:

```yaml
target:
  connection:
    security_users:
      - user: "app_user"
        host: "%"
```

### Retry and Circuit Breaker

Defaults are shown in the examples. Adjust for flaky networks:
- `max_retries` — total retry attempts
- `base_delay_seconds` — initial backoff (exponential with jitter)
- `max_delay_seconds` — cap on backoff
- `circuit_breaker_max_failures` — open circuit after N consecutive failures
- `circuit_breaker_reset_timeout_seconds` — wait before half-open test

### Validation Modes

| Mode | Description | Speed |
|------|-------------|-------|
| `count` | `SELECT COUNT(*)` on source and target | Fast |
| `checksum` | MD5 hash of row samples (in-database) | Medium |
| `full` | Row-by-row comparison (pulls all data) | Slow |

`sample_size` applies only to `checksum` mode.

---

## Prerequisites Before Running

- Python 3.11+ installed
- `pip install -e ".[dev]"` (or `pip install .` for non-editable)
- Database engines installed and accessible:
  - MSSQL: SQL Server 2016+ with **ODBC Driver 18 for SQL Server** (OS component)
  - PostgreSQL: 15+ with `listen_addresses = '*'` and `pg_hba.conf` allowing host access
  - MySQL: 8.0+ with appropriate authentication plugin
- Network connectivity: host IP reachable, ports open (1433, 5432, 3306 defaults)
- Container runtime (optional): **Podman** (verified) or **Docker Desktop** (WSL2)

### Container-to-Host Networking

When running the platform in a container, `localhost` inside the container refers
to the container itself, not your host machine.

| Runtime | Host Address |
|---------|--------------|
| Docker Desktop (Windows/Mac) | `host.docker.internal` |
| Podman / Linux Docker | Gateway IP (often `172.17.0.1`) or host's LAN IP |

Update the `host` value in your config accordingly.

---

## Verifying an Example

The example YAML files use placeholder syntax (`<...>`) which is **not valid YAML**.
You must replace placeholders with actual values before the file can be parsed.

To validate your edited config:
```powershell
python -c "import yaml; yaml.safe_load(open('config/my_mssql_migration.yaml'))"
```

---

## Links

- MSSQL authoritative runbook: `../mssql/MSSQL_E2E_RUNBOOK.md`
- PostgreSQL authoritative runbook: `../postgresql/POSTGRESQL_E2E_RUNBOOK.md`
- Acceptance E2E workflow (Mode 2): `../e2e_validation/ACCEPTANCE_RUNBOOK.md`
- Configuration schema: `../../config/migration_config.schema.yaml`
- Secrets documentation: `../../azure_keyvault.md`

---

*These examples are derived from the repository's actual E2E configurations
(`config/mssql_local_test.yaml`, `config/postgresql_local_continer.yaml`,
`config/mysql_local_*.yaml`, and acceptance configs) with machine-specific
values replaced by placeholders.*