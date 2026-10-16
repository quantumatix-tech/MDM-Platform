# Migration Platform — Environment Setup

## Current Windows Laptop — Historical Record (2026-10-12)

The following records the environment where E2E tests were previously verified.
**These are not universal requirements** — use the [config examples](config-examples/)
and [quickstart.md](quickstart.md) for portable setup instructions.

| Component | Value (This Laptop) |
|-----------|---------------------|
| OS | Windows 10/11 |
| Python | 3.12.6 (requires ≥3.11) |
| Git | Installed |
| Podman | 5.x (machine/WSL2) |
| Docker | NOT installed/verified on this laptop |
| MSSQL | SQL Server 2022 (named instance `DMSQL`) on `172.24.48.1:1533` |
| PostgreSQL | PostgreSQL 15/17 on `172.24.48.1:55432` (Windows service `PostgreSQLAutomation`) |
| MySQL | MySQL 8.0.35 on `172.24.48.1:33062` |

### Previously Verified E2E Configurations (This Laptop)

| Engine | Config File | Source DB | Target DB |
|--------|-------------|-----------|-----------|
| MSSQL | `config/mssql_local_test.yaml` | `mssql_migration_test` | `mssql_migration_target` |
| PostgreSQL | `config/postgresql_local_continer.yaml` | `MigrationE2E_PostgreSQL_Source` | `MigrationE2E_PostgreSQL_Target` |
| MySQL (blocked) | `config/mysql_local_usertest.yaml` | `MigrationE2E_MySQL_Source` | `MigrationE2E_MySQL_Target` |

### Secret Environment Variables (This Laptop)

```powershell
# MSSQL
$env:SECRET_mssql_source_pass = "<sa-password>"
$env:SECRET_mssql_target_pass = "<sa-password>"

# PostgreSQL
$env:SECRET_postgresql_e2e_source_pass = "<postgres-password>"
$env:SECRET_postgresql_e2e_target_pass = "<postgres-password>"

# MySQL (blocked)
$env:SECRET_mysql_e2e_source_pass = "<migration_e2e-password>"
$env:SECRET_mysql_e2e_target_pass = "<migration_e2e-password>"
```

Never put real passwords in YAML files. The `secrets.provider: env` reads `SECRET_<name>` at runtime.

---

## Fresh Windows Laptop — Prerequisites Checklist

1. **Git** — clone repository
2. **Python 3.11+** — install from python.org or winget
3. **Virtual environment** (recommended)
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
4. **Dependencies**
   ```powershell
   pip install -e ".[dev]"
   ```
5. **Database Engines** — install and configure:
   - MSSQL: SQL Server 2016+ + ODBC Driver 18 for SQL Server (OS component)
   - PostgreSQL: 15+ with `listen_addresses = '*'`, `pg_hba.conf` for host access
   - MySQL: 8.0+ with `caching_sha2_password` authentication
6. **Windows Firewall** — allow inbound on database ports (default: 1433, 5432, 3306)
7. **Container Runtime** — Podman (with machine/WSL2) OR Docker Desktop (WSL2)
8. **Determine Host IP** — run `ipconfig` and use the actual LAN IP

---

## Configuration Files

### Reusable Examples (Recommended for New Setups)

See `docs/e2e/config-examples/` for portable templates:
- `mssql-example.yaml`
- `postgresql-example.yaml`
- `mysql-example.yaml`

Copy an example to `config/`, replace placeholders, and set secrets.

### Existing Repository Configs

| File | Purpose |
|------|---------|
| `config/mssql_local_test.yaml` | MSSQL local E2E (this laptop) |
| `config/postgresql_local_continer.yaml` | PostgreSQL local E2E (this laptop, filename preserved) |
| `config/mysql_local_usertest.yaml` | MySQL local E2E SSL (this laptop) |
| `config/mysql_e2e_acceptance.yaml` | MySQL acceptance test (root user, no SSL) |
| `config/postgresql_e2e_acceptance.yaml` | PostgreSQL acceptance test |
| `config/mssql_e2e_acceptance.yaml` | MSSQL acceptance test |

---

## Verify Connectivity Before Migration

Replace `<HOST_IP>`, `<PORT>`, `<DATABASE>`, `<USER>` with your values.

```powershell
# MSSQL (requires ODBC Driver 18)
sqlcmd -S <HOST_IP>,<PORT> -U <USER> -P <password> -Q "SELECT @@VERSION"

# PostgreSQL
psql -h <HOST_IP> -p <PORT> -U <USER> -d <DATABASE> -c "SELECT version()"

# MySQL
mysql -h <HOST_IP> -P <PORT> -u <USER> -p --ssl-mode=REQUIRED -e "SELECT VERSION()"
```

---

## Container Runtime

For complete container build, run, networking, and troubleshooting instructions, see
[container-runtime.md](container-runtime.md).

**Quick summary for this laptop:**

- **Podman (verified on this laptop):** `podman build -f docker\Dockerfile -t localhost/migration-platform:0.2.3 .`
- **Docker Desktop (NOT verified on this laptop):** `docker build -t migration-platform -f docker/Dockerfile .`
- Config mounted at runtime: `-v "${PWD}/config:/app/config:ro"`
- Secrets via `-e SECRET_...`; never baked into the image
- Podman/Docker use separate image stores — rebuild when switching runtimes

Example run (adapt config path):
```powershell
podman run --rm `
  -v "${PWD}/config:/app/config:ro" `
  -e SECRET_source_db_pass="<password>" `
  -e SECRET_target_db_pass="<password>" `
  localhost/migration-platform:0.2.3 `
  --config /app/config/mssql-example.yaml --mode full
```

---

## Report and Log Locations

- Audit logs: `logs/<run_id>.jsonl`
- HTML reports: `reports/<run_id>.html`
- JSON reports: `reports/<run_id>.json`
- Runtime manifests: `reports/manifest_<run_id>.json`

---

## Current E2E Status (This Laptop)

| Engine | Status | Last Run ID |
|--------|--------|-------------|
| MSSQL | **VERIFIED** | `905ce6fa6fc743e7b3e86c508448c1c7` |
| PostgreSQL | **VERIFIED** | `c4405c596be5479fa82f2759299410c8` |
| MySQL | **BLOCKED** | `938b096eee6c49978af4e624c08a8775` |

## MySQL Blockers & Troubleshooting

For general troubleshooting guidance (connectivity, SSL/auth, containers, logs/reports, limitations vs environment issues), see [troubleshooting.md](troubleshooting.md).

**MySQL blockers** (local E2E, historical record — do not generalize to acceptance tests):
1. `sha256_password requires SSL` — account needs SSL, host mismatch for localhost
2. `security_users` YAML not read by code (reads top-level, not nested)
3. Cannot read `mysql.user` — needs global SELECT or alternative approach

## Unknown / NOT VERIFIED

- Exact PostgreSQL `pg_hba.conf` rules and `listen_addresses` values
- Windows Firewall rule names for each engine
- Whether Docker Desktop works equivalently on this laptop
- Fresh laptop IP address and port availability
- MySQL SSL certificate configuration details

See [troubleshooting.md](troubleshooting.md) for additional notes on unverified assumptions.

---

*Historical record as of 2026-10-12. No secrets included. No code or config modified.*