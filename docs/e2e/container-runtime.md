# Migration Platform — Container Runtime Guide

## 1. Purpose and Scope

This document describes how to build and run the Migration Platform inside a container using Docker or Podman. It covers:

- Building the reproducible container image from `docker/Dockerfile`
- Running the Migration Platform CLI inside the container
- Supplying configuration and secrets at runtime (no secrets baked into the image)
- Container-to-database networking considerations
- Verified and unverified behaviors for each runtime

**Scope:** This guide covers the Migration Platform application container only. It does not cover running databases in containers (see `docker/docker-compose.test.yml` for a test-only database stack).

## 2. Prerequisites

- **Container runtime:** Docker Engine (Docker Desktop on Windows/macOS) or Podman
- **Repository clone:** Working copy of the Migration Platform repository
- **Configuration file:** A valid YAML migration config (see `docs/e2e/config-examples/`)
- **Secrets:** Database passwords supplied via environment variables at runtime
- **Network access:** Database servers reachable from the container (see Section 7)

**Note:** The container image includes Python 3.11.9, all pinned Python dependencies from `uv.lock`, `unixodbc`, and the Microsoft ODBC Driver 18 for SQL Server. No additional Python or OS packages are required on the host.

## 3. Docker vs. Podman — Project-Relevant Differences

| Aspect | Docker (Docker Desktop) | Podman |
|--------|-------------------------|--------|
| **Image store** | Separate local image store | Separate local image store |
| **Host networking alias** | `host.docker.internal` resolves to host | No built-in alias; use gateway IP (`172.17.0.1` typically) or host LAN IP |
| **Rootless** | Requires Docker Desktop VM | Native rootless support on Linux |
| **Compose** | `docker compose` (built-in) | `podman-compose` (separate install) or `podman play kube` |
| **Verified on this laptop** | **NOT VERIFIED** | **VERIFIED** (Podman 4.x) |

**Key implication:** Images built with `docker build` are not visible to `podman images` and vice versa. Build or pull the image in the runtime you intend to use.

## 4. Confirming the Runtime Is Installed and Running

```powershell
# Docker
docker version
docker info

# Podman
podman version
podman info
```

Both should report a running daemon/engine. If the daemon is not running, start the respective service.

## 5. Building the Image

The Dockerfile uses a two-stage build:

1. **Builder stage** — Installs `uv 0.5.26`, exports hashed requirements from `uv.lock`, installs dependencies with hash verification, and installs the project with `--no-deps`.
2. **Runtime stage** — Minimal `python:3.11.9-slim` base with `unixodbc` and `msodbcsql18` (ODBC Driver 18 for MSSQL). Copies the full `/usr/local` from builder, so all dependencies and the project's `dist-info` are present.

### Build Commands

```powershell
# Docker (NOT verified on this laptop)
docker build -t migration-platform -f docker/Dockerfile .

# Podman (VERIFIED on this laptop)
podman build -f docker/Dockerfile -t localhost/migration-platform:0.2.3 .
```

**Tagging notes:**
- Docker example uses `migration-platform` (no registry prefix).
- Podman example uses `localhost/migration-platform:0.2.3` (explicit tag matching the project version).
- Use a tag that identifies the version (e.g., `migration-platform:0.2.3`).

### Verifying the Build

```powershell
# Docker
docker images migration-platform

# Podman
podman images localhost/migration-platform
```

Run the built-in smoke test (no arguments triggers the entrypoint smoke test):

```powershell
# Docker
docker run --rm migration-platform

# Podman
podman run --rm localhost/migration-platform:0.2.3
```

Expected output includes the package version and runtime manifest details (Python version, key package versions).

## 6. Running the Container

The entrypoint (`docker/entrypoint.sh`) forwards all arguments to `python -m migration_platform`. When no arguments are supplied, it runs a startup smoke test first.

### Basic CLI Commands

```powershell
# Docker
docker run --rm migration-platform --version
docker run --rm migration-platform --help

# Podman
podman run --rm localhost/migration-platform:0.2.3 --version
podman run --rm localhost/migration-platform:0.2.3 --help
```

### Running a Migration

The application configuration is **not** baked into the image. Mount the host `config/` directory (or a specific YAML file) at runtime and supply secrets through environment variables.

```powershell
# Docker (NOT verified on this laptop)
docker run --rm `
    -v "${PWD}/config:/app/config:ro" `
    -v "${PWD}/logs:/app/logs" `
    -v "${PWD}/reports:/app/reports" `
    -e SECRET_source_db_pass="<your-source-password>" `
    -e SECRET_target_db_pass="<your-target-password>" `
    migration-platform `
    --config /app/config/my_mssql.yaml --mode full --no-live-ui

# Podman (VERIFIED on this laptop)
podman run --rm `
    -v "${PWD}/config:/app/config:ro" `
    -v "${PWD}/logs:/app/logs" `
    -v "${PWD}/reports:/app/reports" `
    -e SECRET_source_db_pass="<your-source-password>" `
    -e SECRET_target_db_pass="<your-target-password>" `
    localhost/migration-platform:0.2.3 `
    --config /app/config/my_mssql.yaml --mode full --no-live-ui
```

**Mount explanations:**
- `-v "${PWD}/config:/app/config:ro"` — Mounts the host `config/` directory read-only at `/app/config` inside the container. The config file path passed to `--config` must be the **container path** (e.g., `/app/config/my_mssql.yaml`), not the host path.
- `-v "${PWD}/logs:/app/logs"` — Mounts host `logs/` for audit log output (`logs/<run_id>.jsonl`).
- `-v "${PWD}/reports:/app/reports"` — Mounts host `reports/` for HTML/JSON reports and runtime manifests.

### Using `docker-compose.test.yml` (Test Database Stack Only)

The repository includes `docker/docker-compose.test.yml` which starts test instances of PostgreSQL, MySQL, MongoDB, and MSSQL **for integration testing only**. It also defines a `migration-platform` service that depends on all databases being healthy.

```powershell
# From the repository root
docker compose -f docker/docker-compose.test.yml up --build
```

**Limitations:**
- Uses hardcoded test passwords (`test_pass`, `root_pass`, `sa` with `test_pass`).
- Not suitable for production or real migration workloads.
- Database ports are published to host (5432, 3306, 27017, 1433) — may conflict with local database installations.
- This Compose file is for the test suite, not for running arbitrary migrations.

## 7. Mounting Configuration Safely

**Never bake secrets or real config into the image.** The Dockerfile explicitly does not copy `config/` (see `docker/Dockerfile:93-97`).

### Required Mounts

| Host Path | Container Path | Mode | Purpose |
|-----------|----------------|------|---------|
| `./config` | `/app/config` | `ro` | Migration YAML configuration |
| `./logs` | `/app/logs` | `rw` | Audit logs (`<run_id>.jsonl`) |
| `./reports` | `/app/reports` | `rw` | HTML/JSON reports, runtime manifests |

### Secrets via Environment Variables

The default secret provider is `env`, which reads `SECRET_<name>` environment variables.

```yaml
# In your config YAML:
source:
  connection:
    password_secret: source_db_pass
target:
  connection:
    password_secret: target_db_pass
secrets:
  provider: env
```

At runtime:

```powershell
$env:SECRET_source_db_pass = "<actual-source-password>"
$env:SECRET_target_db_pass = "<actual-target-password>"
# Then run container with -e SECRET_source_db_pass -e SECRET_target_db_pass
```

**Other secret providers** (configured in YAML, resolved at runtime):
- AWS Secrets Manager
- GCP Secret Manager
- HashiCorp Vault
- Local Encrypted File
- Azure Key Vault

These do not require mounting secret files into the container.

## 8. Container-to-Database Networking

**Critical:** `localhost` (or `127.0.0.1`) inside the container refers to the container itself, **not** the host machine.

### Reaching a Database on the Host Machine

| Runtime | Host Address to Use | Notes |
|---------|---------------------|-------|
| Docker Desktop (Windows/macOS) | `host.docker.internal` | Built-in DNS name; resolves to host IP |
| Podman (Linux) | Gateway IP (often `172.17.0.1`) | Default bridge gateway; **NEEDS VERIFICATION** on each host |
| Podman (rootless) | Host LAN IP (from `ipconfig`/`ip addr`) | Rootless Podman has no `host.docker.internal` equivalent |
| Any runtime | Database's actual reachable hostname/IP | For remote/cloud databases (Azure, AWS RDS, etc.) |

### Example Config for Docker Desktop Host Database

```yaml
source:
  engine: postgresql
  connection:
    host: host.docker.internal
    port: 5432
    database: mydb
    username: myuser
    password_secret: source_db_pass
    ssl: true  # Cloud databases; set false for local without TLS
```

### Example Config for Podman Host Database

```yaml
source:
  engine: postgresql
  connection:
    host: 172.17.0.1  # VERIFY this is the correct gateway IP on your host
    port: 5432
    database: mydb
    username: myuser
    password_secret: source_db_pass
    ssl: false  # Local database without TLS
```

**To find the Podman gateway IP:**

```powershell
podman network inspect bridge --format '{{.IPAM.Config}}'
# Or check the default network gateway
```

**Do not assume** `172.17.0.1` works on all Podman setups. Verify with `podman network inspect`.

### Reaching a Database in Another Container

Use the container name (or service name in Compose) as the host. Example with `docker-compose.test.yml`:

```yaml
source:
  connection:
    host: postgres  # service name in docker-compose.test.yml
    port: 5432
```

## 9. Verifying Connectivity and Diagnosing Failures

For full diagnostic guidance (failure modes, firewall, networking edge cases, MySQL/SSL/auth, logs/reports), see [troubleshooting.md](troubleshooting.md).

### Pre-flight Checks (from Host)

```powershell
# MSSQL
sqlcmd -S <HOST_IP>,<PORT> -U <USER> -P <password> -Q "SELECT @@VERSION"

# PostgreSQL
psql -h <HOST_IP> -p <PORT> -U <USER> -d <DATABASE> -c "SELECT version()"

# MySQL (requires SSL for sha256_password accounts)
mysql -h <HOST_IP> -P <PORT> -u <USER> -p --ssl-mode=REQUIRED -e "SELECT VERSION()"
```

- `<HOST_IP>` = LAN IP from `ipconfig` (Windows) or `ip addr` (Linux), **not** `localhost` or `127.0.0.1`
- Verify the service is running, port is listening (`netstat -an` or `ss -ltn`), and TCP path works

### Inside the Container (if shell access needed)

```powershell
# Docker
docker run --rm -it migration-platform sh

# Podman
podman run --rm -it localhost/migration-platform:0.2.3 sh

# Then inside container:
# Test TCP connectivity
nc -zv <HOST> <PORT>
# Or with Python
python -c "import socket; socket.create_connection(('<HOST>', <PORT>), timeout=5)"
```

### Common Failure Modes

| Symptom | Likely Cause | Diagnostic Step |
|---------|--------------|-----------------|
| Connection refused / timeout | Database not running, wrong host/port, firewall | `nc -zv <HOST> <PORT>` from container; check `netstat -an` on host |
| Authentication / SSL failure | Wrong credentials, SSL mode mismatch, host grants | Verify `pg_hba.conf` (PostgreSQL), MySQL account host (`user@'%'`), SQL Server auth mode |
| Image not found | Built in wrong runtime (Docker vs Podman) | `docker images` vs `podman images`; rebuild in target runtime |
| Missing reports | Process exited early, output dirs not mounted | Check exit code; verify `-v` mounts for `logs/` and `reports/` |

### Firewall Guidance

- **Do not disable the firewall globally.**
- Add narrowly scoped inbound rules for database ports if required (1433 MSSQL, 5432 PostgreSQL, 3306 MySQL).
- PostgreSQL on this laptop: verified inbound rule for TCP 55432 — **NEEDS VERIFICATION** of exact rule name/scope.
- MSSQL/MySQL: No verified firewall rules documented.

## 10. Stopping and Removing Containers

Containers started with `--rm` are automatically removed on exit.

```powershell
# Stop a running container (if not using --rm)
docker stop <container_id>
podman stop <container_id>

# Remove stopped containers
docker container prune
podman container prune

# Remove the image
docker rmi migration-platform
podman rmi localhost/migration-platform:0.2.3
```

## 11. Rebuilding After Code or Dependency Changes

| Change Type | Action Required |
|-------------|-----------------|
| Application code (`core/`, `migration_platform/`) | Rebuild image (`docker build` / `podman build`) |
| `pyproject.toml` or `uv.lock` | Regenerate lockfile (`uv lock`), then rebuild image |
| Config YAML only | **No rebuild needed** — config is mounted at runtime |
| Secrets only | **No rebuild needed** — secrets passed via `-e` at runtime |

### Rebuild Commands

```powershell
# Docker
docker build -t migration-platform -f docker/Dockerfile .

# Podman
podman build -f docker/Dockerfile -t localhost/migration-platform:0.2.3 .
```

After rebuilding, run the smoke test to verify:

```powershell
docker run --rm migration-platform
podman run --rm localhost/migration-platform:0.2.3
```

## 12. Verified Behavior and Known Limitations

### Verified (on this laptop, Podman 4.x)

- Image builds successfully from `docker/Dockerfile`
- Entrypoint smoke test passes (imports package, resolves version, reports package versions)
- MSSQL migration runs end-to-end with mounted config and secrets
- PostgreSQL migration runs end-to-end with mounted config and secrets
- Logs and reports written to mounted host directories
- `host.docker.internal` works for Docker Desktop (inferred from upstream documentation; **NOT tested on this laptop**)

### Not Verified / Known Limitations

| Area | Status | Details |
|------|--------|---------|
| Docker Desktop (Windows/macOS) | **NOT VERIFIED** | No successful run recorded on this laptop |
| Podman `host.docker.internal` equivalent | **NOT VERIFIED** | Gateway IP `172.17.0.1` assumed; may differ per host |
| Rootless Podman networking | **NOT VERIFIED** | May require `podman machine` or host LAN IP |
| `docker-compose.test.yml` for real migrations | **NOT SUITABLE** | Hardcoded test passwords; publishes ports that may conflict |
| MySQL in container | **VERIFIED** | Run `d82f18ae5bd34164a8409a2c74911d22` — 7 tables, 33 rows, 0 errors |
| Firewall rules for container egress | **NOT DOCUMENTED** | Host firewall may block container→host traffic |
| SELinux / AppArmor | **NOT TESTED** | May require `:Z` or `:z` volume label on RHEL/Fedora |

### Portability Notes

- The same image tag can be used across machines with the same runtime.
- Images are **not** portable between Docker and Podman local stores.
- Config files with `host.docker.internal` will not work on Podman without modification.
- Always verify the container-to-host networking address for your specific runtime and platform.

---

*Generated from repository evidence as of 2026-10-13. Based on `docker/Dockerfile`, `docker/entrypoint.sh`, `docker/docker-compose.test.yml`, `docker/README.md`, `docs/e2e/quickstart.md`, and `docs/e2e/environment-setup.md`. No secrets included. No application code or configuration modified.*