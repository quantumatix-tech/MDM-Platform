# Docker Runtime

The Migration Platform ships a reproducible container image built from
`docker/Dockerfile`. The same image can run on any machine with Docker (or a
compatible container engine) regardless of the host's Python version or
globally installed Python packages.

## Reproducible dependencies

- `pyproject.toml` is the single source of truth for declared dependencies.
- `uv.lock` pins every direct and transitive dependency to an exact version with
  content hashes. It is generated with `uv lock` (uv 0.5.x) and is committed to
  the repository.
- The Dockerfile consumes the lockfile with `uv export --format
  requirements-txt --no-dev --no-editable --no-header --no-emit-package
  migration-platform` and installs the result with
  `uv pip install --require-hashes`. Hash verification means the build refuses
  to install any package that is not pinned and hashed, so dependencies can
  never silently upgrade or downgrade at build time.
- The project itself is installed separately with `--no-deps` so its own
  dist-info is present for `importlib.metadata.version("migration-platform")`
  at runtime.

To regenerate the lockfile after changing `pyproject.toml`:

```powershell
uv lock
uv lock --check   # exit 0 means the lockfile is up to date
```

## Build

```powershell
docker build -t migration-platform -f docker/Dockerfile .
```

The image is built in two stages:

1. `builder` — installs `uv`, exports the hashed requirements from
   `uv.lock`, installs dependencies with hash verification, and installs the
   project itself.
2. `runtime` — minimal `python:3.11.9-slim` image that also installs the
   Microsoft ODBC Driver 18 (required for MSSQL) and `unixodbc`.

## Run

The image exposes the Migration Platform CLI through `/entrypoint.sh`. When no
command is supplied, the entrypoint runs a startup smoke test (imports the
package, resolves the version, and exercises the runtime manifest helpers)
before launching the CLI. Pass any CLI argument to skip the smoke test.

### Version and help

```powershell
docker run --rm migration-platform --version
docker run --rm migration-platform --help
```

### Run a migration

The application configuration is **not** baked into the image. Mount the host
`config/` directory (or a specific YAML file) at runtime and supply secrets
through environment variables:

```powershell
docker run --rm `
    -v "${PWD}/config:/app/config:ro" `
    -e SECRET_DB_PASSWORD="your-password" `
    migration-platform --config /app/config/my_config.yaml --mode full
```

The CLI arguments are unchanged:

| Argument | Description |
|----------|-------------|
| `--config PATH` | Path to the YAML config file (required). |
| `--mode {full,cdc-incremental,cdc-continuous}` | Migration mode (default `full`). |
| `--port PORT` | Status dashboard port (default `8080`). |
| `--no-live-ui` | Disable the rich terminal UI (raw JSON output). |
| `--version` | Show the version and exit. |
| `--help` | Show help and exit. |

### Secrets

Never bake real credentials into the image. The default secret provider is
`env`, which reads secrets from `SECRET_<NAME>` environment variables. Pass
every required secret into the container with `-e` (or a Docker secret /
compose `env_file`) at runtime. Other supported providers — AWS Secrets
Manager, GCP Secret Manager, HashiCorp Vault, Local Encrypted File, and Azure
Key Vault — are configured through the YAML config and resolved at runtime.

## Networking

`localhost` inside the container means the container itself. It does **not**
refer to the host laptop. To reach a database running on the host machine:

- **Docker Desktop (Linux engine):** use `host.docker.internal` as the host.
- **Podman / other engines:** use the container's gateway IP (often
  `172.17.0.1`) or the host's reachable address.
- **Remote databases:** use the database's reachable host name or IP and port.

Example source connection for a PostgreSQL database on the Docker Desktop
host:

```yaml
source:
  engine: postgresql
  connection:
    host: host.docker.internal
    port: 5432
    database: mydb
    username: myuser
    password_secret: db_password
```

## Required runtime packages

The runtime image keeps only what the application needs:

- Python 3.11.9 (from the pinned base image).
- `unixodbc` and `msodbcsql18` (ODBC Driver 18 for SQL Server) for MSSQL.
- All Python dependencies from the lockfile: PostgreSQL (`psycopg`),
  MySQL (`mysql-connector-python`, `mysql-replication`), MongoDB (`pymongo`),
  MSSQL (`pyodbc`), Cosmos DB (`azure-cosmos`), Azure
  (`azure-identity`, `azure-keyvault-secrets`, `azure-storage-blob`),
  AWS (`boto3`), GCP (`google-cloud-secret-manager`), HashiCorp Vault
  (`hvac`), plus `cryptography`, `keyring`, `rich`, `pyyaml`, and `requests`.