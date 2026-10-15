# Migration Platform — Quick Setup (Windows)

## 1. Current Status (Previously Verified on This Laptop)

| Engine | Config (This Laptop) | Last Run ID | Tables | Rows | Status |
|--------|----------------------|-------------|--------|------|--------|
| MSSQL | `config/mssql_local_test.yaml` | `905ce6fa6fc743e7b3e86c508448c1c7` | 9 | 28 | **VERIFIED** |
| PostgreSQL | `config/postgresql_local_continer.yaml` | `c4405c596be5479fa82f2759299410c8` | 7 | 33 | **VERIFIED** |
| MySQL | `config/mysql_local_usertest.yaml` | `938b096eee6c49978af4e624c08a8775` | 0 | 0 | **BLOCKED** |

**Do not rerun MySQL.** Blockers: `sha256_password requires SSL`, `security_users` YAML misread, `mysql.user` access denied.

> **Use portable config examples for new setups:** See `docs/e2e/config-examples/` for templates with placeholders.

---

## 2. Fresh Windows Laptop — Prerequisites

- Git
- Python 3.11+
- Virtual environment: `python -m venv .venv && .\.venv\Scripts\Activate.ps1`
- Dependencies: `pip install -e ".[dev]"`
- Database engines installed and running:
  - MSSQL: SQL Server 2016+ + **ODBC Driver 18 for SQL Server** (OS component)
  - PostgreSQL: 15+ with `listen_addresses = '*'`, `pg_hba.conf` host access
  - MySQL: 8.0+ with `caching_sha2_password`
- Container runtime: **Podman** (verified) or Docker Desktop (WSL2) — **NEEDS VERIFICATION**
- Determine host LAN IP with `ipconfig` (do not use `172.24.48.1`)

---

## 3. Configuration — Use Portable Examples (Recommended)

Copy an example to `config/`, edit placeholders, set secrets:

```powershell
# MSSQL
Copy-Item docs/e2e/config-examples/mssql-example.yaml config/my_mssql.yaml

# PostgreSQL
Copy-Item docs/e2e/config-examples/postgresql-example.yaml config/my_pg.yaml

# MySQL
Copy-Item docs/e2e/config-examples/mysql-example.yaml config/my_mysql.yaml
```

Edit the new file: replace `<SOURCE_HOST>`, `<SOURCE_DATABASE>`, `<SOURCE_USERNAME>`, `<TARGET_HOST>`, `<TARGET_DATABASE>`, `<TARGET_USERNAME>`, `<SCHEMA_1>`, etc.

Set secrets (PowerShell):
```powershell
$env:SECRET_source_db_pass = "<your-source-password>"
$env:SECRET_target_db_pass = "<your-target-password>"
```

Run:
```powershell
python -m migration_platform --config config/my_mssql.yaml --mode full --no-live-ui
```

### Historical Configs (This Laptop Only)

| Engine | Config File | Source DB | Target DB | Secret Env Vars |
|--------|-------------|-----------|-----------|-----------------|
| MSSQL | `config/mssql_local_test.yaml` | `mssql_migration_test` | `mssql_migration_target` | `SECRET_mssql_source_pass`, `SECRET_mssql_target_pass` |
| PostgreSQL | `config/postgresql_local_continer.yaml` | `MigrationE2E_PostgreSQL_Source` | `MigrationE2E_PostgreSQL_Target` | `SECRET_postgresql_e2e_source_pass`, `SECRET_postgresql_e2e_target_pass` |
| MySQL (blocked) | `config/mysql_local_usertest.yaml` | `MigrationE2E_MySQL_Source` | `MigrationE2E_MySQL_Target` | `SECRET_mysql_e2e_source_pass`, `SECRET_mysql_e2e_target_pass` |

**Never put real passwords in YAML.** Use `SECRET_<name>` env vars (read by `secrets.provider: env`).

---

## 4. Container Runtime

For complete container build, run, networking, and troubleshooting instructions, see
[container-runtime.md](container-runtime.md).

**Quick summary:**

- **Podman (verified on this laptop):** `podman build -f docker/Dockerfile -t localhost/migration-platform:0.2.3 .`
- **Docker Desktop (NOT verified on this laptop):** `docker build -t migration-platform -f docker/Dockerfile .`
- Config is mounted at runtime: `-v "${PWD}/config:/app/config:ro"`
- Secrets via `-e SECRET_...`; never baked into the image
- `localhost` inside container ≠ host; use `host.docker.internal` (Docker) or gateway IP (Podman) — **NEEDS VERIFICATION**

Run a migration in the container (adapt config path):
```powershell
podman run --rm `
  -v "${PWD}/config:/app/config:ro" `
  -v "${PWD}/logs:/app/logs" `
  -v "${PWD}/reports:/app/reports" `
  -e SECRET_source_db_pass="<password>" `
  -e SECRET_target_db_pass="<password>" `
  localhost/migration-platform:0.2.3 `
  --config /app/config/my_mssql.yaml --mode full --no-live-ui
```

---

## 5. Network/Connectivity Checks

Replace `<HOST_IP>`, `<PORT>`, `<USER>`, `<DATABASE>` with your values.

```powershell
# MSSQL
sqlcmd -S <HOST_IP>,<PORT> -U <USER> -P <password> -Q "SELECT @@VERSION"

# PostgreSQL
psql -h <HOST_IP> -p <PORT> -U <USER> -d <DATABASE> -c "SELECT version()"

# MySQL
mysql -h <HOST_IP> -P <PORT> -u <USER> -p --ssl-mode=REQUIRED -e "SELECT VERSION()"
```

- `<HOST_IP>` = LAN IP from `ipconfig`, NOT `localhost` or `127.0.0.1`
- Container-to-host networking: see [container-runtime.md](container-runtime.md#container-to-database-networking)
- Verify service is running, port is listening (`netstat -an`), and TCP path works

---

## 6. Firewall

- **PostgreSQL**: Verified inbound rule for TCP 55432 — `NEEDS VERIFICATION` exact rule name/scope
- **MSSQL/MySQL**: No verified firewall rules documented. **Do not disable firewall globally.** Add narrowly scoped inbound rules if required.

---

## 7. After Code Changes

1. Run relevant unit tests: `pytest tests/unit/test_runtime_manifest.py tests/unit/test_report_manifest.py -v`
2. Rebuild image if application code changed
3. Run selected E2E config
4. Check: exit code, run ID, `logs/<run_id>.jsonl`, `reports/<run_id>.html`, `reports/<run_id>.json`
5. Independently validate target row counts and required objects

---

## 8. Troubleshooting

For detailed troubleshooting (connectivity, SSL/auth, containers, MySQL blockers vs acceptance, logs/reports, limitations), see [troubleshooting.md](troubleshooting.md).

**Quick mini-table:**

| Symptom | Check |
|---------|-------|
| Connection refused/timeout | Service running? Correct IP? Port listening? Firewall? TCP path container→host? |
| Authentication/SSL failure | Account host grants? Credentials match? SSL mode matches server config? |
| Image not found | Built/pulled in the correct runtime (Podman vs Docker)? |
| Missing report | Process exit status? Output dirs mounted (`logs/`, `reports/`)? |

---

*Generated from verified repository state 2026-10-12. No secrets, no code/config/db modifications.*