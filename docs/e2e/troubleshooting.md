# Migration Platform — E2E Troubleshooting Guide

## 1. Quick Diagnosis

| Symptom | Likely Cause | Quick Check |
|---------|--------------|-------------|
| Connection refused / timeout | DB not running, wrong IP/port, firewall blocking | Test connectivity with `sqlcmd`/`psql`/`mysql` to host IP (not `localhost` for host→container) |
| Authentication failure | Wrong credentials, missing `SECRET_*`, account host grants | Verify env vars set, check `pg_hba.conf` (Postgres), MySQL host grants, MSSQL auth mode |
| SSL/TLS failure | `ssl` mismatch (default is `true`), no TLS needed for local | Check config: local DB without TLS → `ssl: false`; cloud → `ssl: true` |
| Image not found (container) | Built in different runtime (Docker vs Podman) | Check `docker images` vs `podman images`; build in target runtime |
| Missing reports/logs | Process exited early, dirs not mounted | Verify mounts for `logs/` and `reports/`; check container exit code |
| ODBC Driver missing | ODBC Driver 18 for SQL Server not installed | `sqlcmd` or migration logs show driver error; install ODBC Driver 18 |

## 2. Host/Database Connection Failures

- **Connect using host LAN IP:** Replace `<HOST_IP>` from `ipconfig` (Windows) — never use `localhost` or `127.0.0.1` when describing host from container or cross-machine access.
- **Ports:** Default ports: MSSQL 1433, PostgreSQL 5432, MySQL 3306. On this laptop, historical ports differ (1533, 55432, 33062) — these are environment-specific and must be preserved as-is where referenced.
- **Pre-flight:** Use `sqlcmd`, `psql`, or `mysql` to verify TCP reachability before running migration (see [container-runtime.md#9-verifying-connectivity-and-diagnosing-failures](container-runtime.md#verifying-connectivity-and-diagnosing-failures)).
- **Firewall:** Do **not** disable firewall globally. Add narrowly scoped inbound rules for DB ports. PostgreSQL on this laptop had a documented inbound rule for TCP 55432 — treat as environment-dependent (exact rule name/scope needs verification).

## 3. Container Networking

- **Principle:** `localhost` inside a container refers to the container itself, not the host.
- **Docker Desktop:** Use `host.docker.internal` to reach host databases.
- **Podman:** No built-in `host.docker.internal` in all setups. Use the bridge gateway IP (often `172.17.0.1`) or host LAN IP. **This is NOT verified on this laptop** — rootless Podman may require different addressing. Always verify for your environment.
- **Separate image stores:** Docker and Podman use separate local image stores — rebuild in the runtime you will use.
- **Full details:** See [container-runtime.md](container-runtime.md#8-container-to-database-networking).

## 4. SSL/TLS and Authentication/Secrets

- **Secrets:** Never put real passwords in YAML. Use `SECRET_<name>` env vars with `secrets.provider: env`. Other providers are configured in YAML and resolved at runtime.
- **SSL defaults:** The platform defaults `ssl: true` for all engines. Set `ssl: false` for local databases without TLS. SSL/TLS behavior differs by engine (e.g., MSSQL `trust_server_certificate` is derived from `ssl`).
- **MSSQL:** Requires ODBC Driver 18 for SQL Server. SSL details: see config examples and engine docs.
- **PostgreSQL:** Check `listen_addresses = '*'` and `pg_hba.conf` host access rules.
- **MySQL:** Authentication plugins (e.g., `sha256_password`, `caching_sha2_password`) affect SSL requirements. See MySQL-specific section below.

## 5. MySQL: Local E2E and Acceptance-Test Results

**Important distinction:** Local E2E results and acceptance-test results must be treated separately.

- **Local E2E (this laptop) — VERIFIED (run `d82f18ae5bd34164a8409a2c74911d22`):** `config/mysql_local_02.yaml` completed with **SUCCESS**. 7/7 tables, 33/33 rows, 0 failed rows, 0 errors. Functions (`fn_GetOrderTotal`, `sp_UpdateProductStock`), trigger (`tr_AuditOrder`), comments, grants, and count validation all succeeded.

  **Resolved historical issues (for context only):**
  1. `sha256_password requires SSL` — resolved by using TLS with CA cert (`ssl: true`, `ca_path: /app/certs/mysql_ca.pem`)
  2. `security_users` YAML not read by code — resolved in `migration_platform/__main__.py` and `core/connectors/mysql/objects/security.py` (merges/falls back to `migration.security_users`)
  3. `mysql.user` access denied — resolved by using the `security_users` allowlist, avoiding the `mysql.user` query
  4. `SHOW_ROUTINE` missing — resolved by granting `SHOW_ROUTINE` to the migration account
  5. `log_bin_trust_function_creators = OFF` — resolved by `SET PERSIST log_bin_trust_function_creators = ON` (requires DBA approval)

- **Acceptance E2E (Mode 2):** Accepts `root` user without SSL in the tested acceptance config (`config/mysql_e2e_acceptance.yaml`) and setup/migration/validation succeeded (7 tables, 33 rows, 100% success) once `mysql` CLI was in `PATH` (environment issue, not migration failure). The acceptance runbook documents this as an environment/`PATH` fix, not a code change.

**When troubleshooting MySQL:**
- Do not generalize historical blockers to all MySQL deployments. Check which path (local E2E vs acceptance) and which config you are using.
- For local TLS, ensure `ssl: true`, `ca_path`, `verify_cert`, and `verify_identity` are configured correctly.
- If using acceptance fixtures, verify `mysql` CLI is in `PATH` (see acceptance runbook diagnostics).
- `SET PERSIST log_bin_trust_function_creators = ON` is a prerequisite only where the target MySQL server has binary logging enabled and the setting is OFF. It requires DBA approval.

## 6. MSSQL & PostgreSQL Troubleshooting

**Reference authoritative engine documentation:**

- MSSQL: [MSSQL_E2E_RUNBOOK.md](../mssql/MSSQL_E2E_RUNBOOK.md), [MSSQL_TEST_GUIDE.md](../mssql/MSSQL_TEST_GUIDE.md), [MSSQL_LIMITATIONS.md](../mssql/MSSQL_LIMITATIONS.md)
- PostgreSQL: [POSTGRESQL_E2E_RUNBOOK.md](../postgresql/POSTGRESQL_E2E_RUNBOOK.md), [POSTGRESQL_TEST_GUIDE.md](../postgresql/POSTGRESQL_TEST_GUIDE.md), [POSTGRESQL_LIMITATIONS.md](../postgresql/POSTGRESQL_LIMITATIONS.md)

**Common checks (engine-agnostic) drawn from existing docs:**
- Verify DB version compatibility, correct ports/hosts, and required grants
- Reset target/database fixtures when re-running to avoid pre-existing-object conflicts (object already exists errors)
- Dependency ordering matters for FKs/views/triggers; ensure `include_schemas` is complete
- Check for environment-specific prerequisites (e.g., PostgreSQL `wal_level = logical` for CDC — environment prerequisite, not migration bug)
- Azure-specific: MSSQL requires SSL; firewall must allow client IP (error 4060) — environment issues, not implementation bugs

## 7. Runtime/Dependency & Configuration-Path Issues

- **Python/venv:** Use Python 3.11+. Create `.venv` and `pip install -e ".[dev]"`.
- **ODBC Driver:** MSSQL requires **ODBC Driver 18 for SQL Server** (OS component). Not a Python package. Install per OS; Docker image already includes it.
- **Docker image store split:** Podman vs Docker are separate; rebuild when switching. Image tags differ (e.g., `localhost/migration-platform:0.2.3` for Podman).
- **Config paths:** Config is mounted at runtime in container (`/app/config/...`), not baked into image. YAML must be valid; validate with `python -c "import yaml; yaml.safe_load(open('config/my_config.yaml'))"` if parsing issues arise.
- **Acceptance compose reference:** `docker-compose.e2e.yaml` is referenced in acceptance runbook but **does not exist** in the repository. Use `docker/docker-compose.test.yml` as a test-stack reference only — do not invent `docker-compose.e2e.yaml`.
- **mysql CLI in PATH:** Acceptance MySQL setup fails if `mysql` CLI not found — fix PATH (environment issue, not migration failure). Documented in acceptance runbook with exact diagnosis/verification steps.

## 8. Logs, Reports, and Failure Diagnosis

- **Report/log locations:**
  - Audit logs: `logs/<run_id>.jsonl`
  - HTML reports: `reports/<run_id>.html`
  - JSON reports: `reports/<run_id>.json`
  - Runtime manifests: `reports/manifest_<run_id>.json`
- **After a failed run:** Check exit code, run ID, and the above files. Inspect failed objects/errors reported in CLI output (and in reports). Look for driver/auth/SSL/network errors first.
- **Container smoke test:** Running container with no args triggers entrypoint smoke test (imports, version, package versions) — useful to validate image build/runtime packages.
- **Post-migration validation:** Independently validate target row counts and required objects. For acceptance, use validation matrix and check phases as documented.

## 9. Platform Limitations vs Environment Problems

| Type | Examples | Action |
|------|----------|--------|
| **Implementation limitation** | PostgreSQL: non-public types skipped (public-schema hardcoded), trigger enabled/disabled state not migrated, FORCE RLS not migrated, RLS polroles not migrated, partition schema handling in some cases | Check authoritative limitations docs; treat as known gaps, not "bugs to workaround blindly". Link to relevant LIMITATIONS.md. |
| **Environment/prerequisite** | PostgreSQL `wal_level = logical`, Windows Firewall rules, MySQL CLI PATH, Azure firewall (error 4060), SSL cert setup | Fix in environment. These are not migration implementation failures. Documented as environment issues. |
| **Pre-existing target state** | Target DB has objects/rows from prior runs causing "object already exists" (e.g., Azure SQL functions/procs), partitioned table row-count mismatches before clean run | Reset/clean target before FULL migration for deterministic results. |
| **Audit scope/validation** | Trigger enabled/disabled exhaustive validation incomplete, partition data comparison needs clean state | Note as validation scope; don't assume verification without evidence. |

## 10. Unverified Assumptions & Evidence Gaps

- **Container→host networking (Podman):** Gateway IP `172.17.0.1` and rootless Podman behavior are **NOT verified** on this laptop. `host.docker.internal` is **NOT verified** for Docker Desktop on this laptop. Mark as environment-dependent.
- **Docker Desktop equivalence:** No evidence of successful runs on this laptop; treat Docker commands as examples/examples only unless proven.
- **Firewall rule details:** Exact Windows Firewall rule names/scope for each engine are **NOT VERIFIED** (only PostgreSQL TCP 55432 is noted historically). Do not assume global firewall disable.
- **Fresh laptop specifics:** Exact `pg_hba.conf`, `listen_addresses` values, and port availability on a fresh laptop are **NOT VERIFIED**.
- **MySQL SSL certs:** SSL certificate configuration details for `sha256_password` are **NOT VERIFIED**.
- **`docker-compose.e2e.yaml`:** Referenced by acceptance runbook but missing from repo — **confirmed absent**. Do not create or reference as existing.
- **Acceptance flow distinction:** MySQL success (root, no SSL) in acceptance is documented as a successful environment/path fix; this **must remain distinct** from local E2E SSL/blockers.

## 11. Where to Link This Guide

Add `troubleshooting.md` to [docs/e2e/README.md](README.md) Documentation Index. Link from:
- [quickstart.md](quickstart.md) — replace the mini-troubleshooting table with a short link to this guide
- [environment-setup.md](environment-setup.md) — link to relevant sections (connectivity, container, MySQL blockers)
- [container-runtime.md](container-runtime.md) — link to connectivity/networking troubleshooting sections
- [ACCEPTANCE_RUNBOOK.md](../e2e_validation/ACCEPTANCE_RUNBOOK.md) — keep engine-specific troubleshooting but link to this guide for general networking/secrets
- Engine runbooks (`../mssql/MSSQL_E2E_RUNBOOK.md`, `../postgresql/POSTGRESQL_E2E_RUNBOOK.md`) — link to this guide for container/networking/secrets; keep engine-specific details authoritative

---

*Read-only: based on existing repository evidence and findings already captured. No historical records were rewritten; unverified items are clearly marked.*