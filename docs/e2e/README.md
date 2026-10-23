# Migration Platform — E2E Testing and Reproducibility Documentation

Documentation for reproducing end-to-end migration tests on MSSQL, PostgreSQL, and MySQL.

## Quick Start — Portable (Recommended)

Use the [config examples](config-examples/) for a fresh setup:

1. Copy an example to `config/`:
   ```powershell
   Copy-Item docs/e2e/config-examples/mssql-example.yaml config/my_mssql.yaml
   ```
2. Edit placeholders (`<SOURCE_HOST>`, `<SOURCE_DATABASE>`, etc.) with your values
3. Set secrets:
   ```powershell
   $env:SECRET_source_db_pass = "<your-password>"
   $env:SECRET_target_db_pass = "<your-password>"
   ```
4. Run:
   ```powershell
   python -m migration_platform --config config/my_mssql.yaml --mode full --no-live-ui
   ```

---

## Quick Start — This Laptop (Historical, 2026-10-12)

The following were previously verified on a specific development laptop. They use
machine-specific IPs, ports, and database names. **Do not copy directly** — use
the portable examples above for a new environment.

### MSSQL (verified 100% success)
```powershell
$env:SECRET_mssql_source_pass = "<sa-password>"
$env:SECRET_mssql_target_pass = "<sa-password>"
python -m migration_platform --config config/mssql_local_test.yaml --mode full --no-live-ui
```
Run ID: `905ce6fa6fc743e7b3e86c508448c1c7` — 9 tables, 28 rows, 0 errors

### PostgreSQL (verified 100% success)
```powershell
$env:SECRET_postgresql_e2e_source_pass = "<postgres-password>"
$env:SECRET_postgresql_e2e_target_pass = "<postgres-password>"
python -m migration_platform --config config/postgresql_local_continer.yaml --mode full --no-live-ui
```
Run ID: `c4405c596be5479fa82f2759299410c8` — 7 tables, 33 rows, 0 errors

### MySQL (verified SUCCESS)
See [environment-setup.md](environment-setup.md) for current status and diagnostic steps.

---

## Documentation Index

| Document | Purpose |
|----------|---------|
| [environment-setup.md](environment-setup.md) | Prerequisites, historical laptop record, connectivity checks |
| [quickstart.md](quickstart.md) | Shortest path to build and run a migration |
| [container-runtime.md](container-runtime.md) | Container build, run, networking, and troubleshooting |
| [troubleshooting.md](troubleshooting.md) | Common E2E issues, diagnostics, and unverified assumptions |
| [config-examples/README.md](config-examples/README.md) | Reusable config templates with explanations |
| [MSSQL E2E Runbook](../mssql/MSSQL_E2E_RUNBOOK.md) | MSSQL E2E: prerequisites, commands, validation, cleanup |
| [PostgreSQL E2E Runbook](../postgresql/POSTGRESQL_E2E_RUNBOOK.md) | PostgreSQL E2E: service setup, host/firewall/pg_hba, validation |

> **Note**: `MSSQL_E2E_RUNBOOK.md` and `POSTGRESQL_E2E_RUNBOOK.md` are the authoritative runbooks in their respective engine directories.
> MySQL E2E is now VERIFIED (run `d82f18ae5bd34164a8409a2c74911d22`); see `docs/e2e/environment-setup.md`.

---

## Status Summary — Historical (This Laptop)

| Engine | Config (This Laptop) | Last Verified Run | Tables | Rows | Status |
|--------|----------------------|-------------------|--------|------|--------|
| MSSQL | `config/mssql_local_test.yaml` | `905ce6fa6fc743e7b3e86c508448c1c7` | 9 | 28 | **VERIFIED** |
| PostgreSQL | `config/postgresql_local_continer.yaml` | `c4405c596be5479fa82f2759299410c8` | 7 | 33 | **VERIFIED** |
| MySQL | `config/mysql_local_02.yaml` | `d82f18ae5bd34164a8409a2c74911d22` | 7 | 33 | **VERIFIED** |

> **Status Legend**:
> - **VERIFIED** — Successful run with evidence on this laptop
> - **BLOCKED** — Known unresolved blocker prevents success
> - **NOT VERIFIED** — No current reproducible result available
> - **PENDING** — Check remains to be performed

---

## Safety Rules

- **Never** commit real passwords to YAML or documentation. Use `SECRET_<name>` environment variables.
- **Never** drop, recreate, or truncate databases during documentation generation.
- **Never** modify application code, YAML configs, SQL fixtures, Dockerfile, or database contents.
- **Mark destructive commands** clearly and require explicit approval.
- Distinguish **VERIFIED**, **BLOCKED**, **NOT VERIFIED**, **PENDING** throughout.
- If evidence is missing, mark `NOT VERIFIED` and explain how to verify.

---

## Report Locations

- **Audit logs (JSONL)**: `logs/<run_id>.jsonl`
- **HTML reports**: `reports/<run_id>.html`
- **JSON reports**: `reports/<run_id>.json`
- **Runtime manifests**: `reports/manifest_<run_id>.json`

---

## Container Image

See [container-runtime.md](container-runtime.md) for complete build and run instructions.

Locally built Podman image: `localhost/migration-platform:0.2.3`
Dockerfile: `docker/Dockerfile`

---

*Documentation reflects repository state as of 2026-10-12. No application code, configuration, database, or Git state was modified during documentation creation.*